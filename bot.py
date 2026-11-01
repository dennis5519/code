import asyncio
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord.ext import commands, tasks
from dotenv import load_dotenv
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

load_dotenv()

TG_CHANNEL = os.getenv("TG_CHANNEL", "shuffleboost").strip().lstrip("@")
POLL_INTERVAL_SECONDS = max(10, int(os.getenv("POLL_INTERVAL_SECONDS", "10")))
BACKFILL_MAX_PAGES = max(1, int(os.getenv("BACKFILL_MAX_PAGES", "10")))
POLL_CATCHUP_MAX_PAGES = max(2, int(os.getenv("POLL_CATCHUP_MAX_PAGES", "20")))
FORUM_CODE_RECOVERY_POSTS = 5
FORUM_RESPONSE_MAX_BYTES = 1_048_576
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "shufflebot.sqlite3"))
STREAM_STATUS_TEXT = os.getenv("STREAM_STATUS_TEXT", "> yubin12 코드로 방송중 <")
STREAM_STATUS_URL = os.getenv("STREAM_STATUS_URL", "https://www.twitch.tv/yubin12")
ADMIN_IDS = {
    int(user_id.strip())
    for user_id in os.getenv("ADMIN_IDS", "").split(",")
    if user_id.strip().isdigit()
}
TELEGRAM_URL = f"https://t.me/s/{TG_CHANNEL}"
KST = timezone(timedelta(hours=9))

CODE_RE = re.compile(r"(?m)^\s*(?=[A-Z0-9]{8}\s*$)(?=.*[A-Z])(?=.*\d)[A-Z0-9]{8}\s*$")
FORUM_CODE_LINE_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(\d{1,2})(?:ST|ND|RD|TH)\s+([A-Z]+)\s*:\s*[\"“]?([A-Z0-9]{8})[\"”]?\s*$"
)
BONUS_RE = re.compile(
    r"\$([\d,]+(?:\.\d{1,2})?)\s+for\s+the\s+first\s+([\d,]+)\s+users?\b.*?"
    r"\$([\d,]+(?:\.\d{1,2})?)\s+wager\s+requirement\s+past\s+([\d,]+)\s+days?",
    re.IGNORECASE | re.DOTALL,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
)
log = logging.getLogger("shuffle-bot")


def parse_posts(html: str, channel: str = TG_CHANNEL) -> list[dict]:
    """Parse Telegram public-preview messages without depending on bot state."""
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for message in soup.select("div.tgme_widget_message[data-post]"):
        post_ref = message.get("data-post", "").rsplit("/", 1)
        if len(post_ref) != 2 or not post_ref[1].isdigit():
            continue
        body = message.select_one(".tgme_widget_message_text")
        if body is None:
            continue

        text = body.get_text("\n", strip=True)
        code_matches = CODE_RE.findall(text)
        if len(code_matches) > 1:
            log.warning("post %s has multiple code candidates; automatic drop skipped", post_ref[1])
        bonus_match = BONUS_RE.search(text)
        date_element = message.select_one("a.tgme_widget_message_date time[datetime]")
        posted_at = None
        if date_element:
            try:
                posted_at = datetime.fromisoformat(date_element["datetime"].replace("Z", "+00:00"))
                if posted_at.tzinfo is None:
                    posted_at = posted_at.replace(tzinfo=timezone.utc)
            except ValueError:
                log.warning("invalid Telegram timestamp for post %s", post_ref[1])

        external_links = []
        for anchor in body.select("a[href]"):
            href = anchor.get("href", "").strip()
            hostname = (urlparse(href).hostname or "").lower()
            if urlparse(href).scheme in {"http", "https"} and hostname and hostname != "t.me" and not hostname.endswith(".t.me"):
                external_links.append((hostname, href))
        benefit_url = next(
            (href for hostname, href in external_links if hostname == "shufflecommunity.com" or hostname.endswith(".shufflecommunity.com")),
            external_links[0][1] if external_links else None,
        )

        bonus_data = bonus_match.groups() if bonus_match else (None, None, None, None)
        post_id = int(post_ref[1])
        posts.append(
            {
                "id": post_id,
                "code": code_matches[0] if len(code_matches) == 1 else None,
                "bonus": bonus_data[0],
                "users": bonus_data[1],
                "wager": bonus_data[2],
                "days": bonus_data[3],
                "posted_at": posted_at,
                "link": benefit_url,
                "url": f"https://t.me/{channel}/{post_id}",
            }
        )
    return sorted(posts, key=lambda post: post["id"])


def extract_landing_page_code(html: str, posted_at: datetime | None) -> str | None:
    if posted_at is None:
        return None
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    allowed_dates = {posted_at.date(), posted_at.astimezone(KST).date()}
    month_numbers = {
        "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4,
        "MAY": 5, "JUNE": 6, "JULY": 7, "AUGUST": 8,
        "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
    }
    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    candidates = set()
    for day_text, month_text, code in FORUM_CODE_LINE_RE.findall(text):
        if not re.fullmatch(r"[A-Z0-9]{8}", code):
            continue
        month = month_numbers.get(month_text.upper())
        if month and any(date.month == month and date.day == int(day_text) for date in allowed_dates):
            candidates.add(code)
    return next(iter(candidates)) if len(candidates) == 1 else None


def is_allowed_landing_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and (hostname == "shufflecommunity.com" or hostname.endswith(".shufflecommunity.com"))
        and parsed.username is None
        and parsed.password is None
    )


class Database:
    def __init__(self, path: Path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_posts (
                post_id INTEGER PRIMARY KEY,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS codes (
                post_id INTEGER PRIMARY KEY,
                code TEXT NOT NULL,
                bonus TEXT,
                users TEXT,
                wager TEXT,
                days TEXT,
                posted_at TEXT,
                benefit_url TEXT,
                post_url TEXT NOT NULL,
                sent_at TEXT
            );
            CREATE TABLE IF NOT EXISTS delivery_outbox (
                post_id INTEGER PRIMARY KEY,
                nonce TEXT NOT NULL,
                state TEXT NOT NULL,
                discord_message_id INTEGER,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER PRIMARY KEY,
                dm_enabled INTEGER NOT NULL DEFAULT 0,
                min_bonus REAL NOT NULL DEFAULT 0
            );
            """
        )
        self.connection.commit()

    def get_setting(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str | int) -> None:
        self.connection.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.connection.commit()

    def delete_setting(self, key: str) -> None:
        self.connection.execute("DELETE FROM settings WHERE key = ?", (key,))
        self.connection.commit()

    def save_post(self, post: dict) -> None:
        self.connection.execute("INSERT OR IGNORE INTO processed_posts(post_id) VALUES(?)", (post["id"],))
        if post.get("code"):
            self.connection.execute(
                """INSERT OR IGNORE INTO codes
                   (post_id, code, bonus, users, wager, days, posted_at, benefit_url, post_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    post["id"], post["code"], post.get("bonus"), post.get("users"),
                    post.get("wager"), post.get("days"),
                    post["posted_at"].isoformat() if post.get("posted_at") else None,
                    post.get("link"), post["url"],
                ),
            )
        self.connection.commit()

    def get_delivery(self, post_id: int):
        return self.connection.execute(
            "SELECT * FROM delivery_outbox WHERE post_id = ?", (post_id,)
        ).fetchone()

    def latest_sent_delivery(self):
        return self.connection.execute(
            """SELECT * FROM delivery_outbox
               WHERE state = 'sent' AND discord_message_id IS NOT NULL
               ORDER BY post_id DESC LIMIT 1"""
        ).fetchone()

    def begin_delivery(self, post_id: int, nonce: str) -> None:
        self.connection.execute(
            """INSERT INTO delivery_outbox(post_id, nonce, state) VALUES(?, ?, 'sending')
               ON CONFLICT(post_id) DO UPDATE SET state='sending', updated_at=CURRENT_TIMESTAMP
               WHERE delivery_outbox.state != 'sent'""",
            (post_id, nonce),
        )
        self.connection.commit()

    def mark_sent(self, post_id: int, message_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                "UPDATE codes SET sent_at = ? WHERE post_id = ?", (now, post_id)
            )
            self.connection.execute(
                """UPDATE delivery_outbox SET state='sent', discord_message_id=?, updated_at=?
                   WHERE post_id=?""",
                (message_id, now, post_id),
            )

    def latest_codes(self, limit: int = 10, offset: int = 0) -> list:
        return self.connection.execute(
            "SELECT * FROM codes ORDER BY post_id DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()

    def get_code(self, post_id: int) -> dict | None:
        row = self.connection.execute("SELECT * FROM codes WHERE post_id = ?", (post_id,)).fetchone()
        return dict(row) if row else None

    def pending_codes_after(self, post_id: int, through_post_id: int) -> list:
        return self.connection.execute(
            """SELECT * FROM codes WHERE post_id > ? AND post_id <= ? AND sent_at IS NULL
               ORDER BY post_id ASC""",
            (post_id, through_post_id),
        ).fetchall()

    def code_count(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM codes").fetchone()[0]

    def preference(self, user_id: int) -> dict:
        row = self.connection.execute(
            "SELECT * FROM user_preferences WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else {"user_id": user_id, "dm_enabled": 0, "min_bonus": 0.0}

    def save_preference(self, user_id: int, dm_enabled: int, min_bonus: float) -> None:
        self.connection.execute(
            """INSERT INTO user_preferences(user_id, dm_enabled, min_bonus) VALUES(?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET dm_enabled=excluded.dm_enabled,
               min_bonus=excluded.min_bonus""",
            (user_id, dm_enabled, min_bonus),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


def parse_amount(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return 0.0


def display_amount(value: str | None) -> str:
    return f"${value}" if value else "정보 없음"


def calculate_backoff_seconds(failures: int, retry_after: str | None = None) -> int:
    delay = min(POLL_INTERVAL_SECONDS * (2 ** min(max(failures, 1), 5)), 900)
    retry_delay = None
    if retry_after is not None:
        try:
            retry_delay = max(0, int(float(retry_after) + 0.999))
        except (ValueError, OverflowError):
            try:
                retry_time = parsedate_to_datetime(retry_after)
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=timezone.utc)
                retry_delay = max(
                    0,
                    int((retry_time - datetime.now(timezone.utc)).total_seconds() + 0.999),
                )
            except (TypeError, ValueError, OverflowError):
                pass
    if retry_delay is not None:
        delay = max(delay, retry_delay)
    return min(delay, 900)


def build_embed(post: dict) -> discord.Embed:
    embed = discord.Embed(title="LIVE CODE / @SHUFFLEBOOST", description="🔥", color=0x2ECC71)
    embed.add_field(name="코드", value=f"`{post['code']}`", inline=False)
    users = f" · 선착순 {post['users']}명" if post.get("users") else ""
    wager_days = f" · 최근 {post['days']}일" if post.get("days") else ""
    embed.add_field(name="보너스", value=f"{display_amount(post.get('bonus'))}{users}", inline=True)
    wager = display_amount(post.get("wager"))
    embed.add_field(name="베팅 요건", value=f"{wager}{wager_days}", inline=True)
    posted_at = post.get("posted_at")
    if isinstance(posted_at, str):
        try:
            posted_at = datetime.fromisoformat(posted_at)
        except ValueError:
            posted_at = None
    if posted_at is None:
        posted_at = datetime.now(timezone.utc)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    kst_time = posted_at.astimezone(KST)
    embed.add_field(name="게시 시각", value=kst_time.strftime("%Y-%m-%d %H:%M KST"), inline=True)
    link = post.get("link")
    embed.add_field(name="혜택 페이지", value=f"[바로 열기]({link})" if link else "정보 없음", inline=False)
    embed.set_footer(text=f"TELEGRAM / @{TG_CHANNEL} / POST #{post['id']}")
    embed.timestamp = posted_at.astimezone(timezone.utc)
    return embed


def format_code_row(row) -> str:
    posted_at = row["posted_at"]
    if posted_at:
        try:
            parsed = datetime.fromisoformat(posted_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            time_text = parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
        except ValueError:
            time_text = "시각 정보 없음"
    else:
        time_text = "시각 정보 없음"
    bonus = display_amount(row["bonus"])
    wager = display_amount(row["wager"])
    return f"`{row['code']}` · 보너스 {bonus} · 베팅 {wager} · {time_text}\n[원문]({row['post_url']})"


class CodeView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="코드 복사", style=discord.ButtonStyle.success, custom_id="shuffle:copy")
    async def copy_code(self, interaction: discord.Interaction, button: discord.ui.Button):
        post_id = getattr(interaction.message, "_shuffle_post_id", None)
        if post_id is None and interaction.message and interaction.message.embeds:
            footer = interaction.message.embeds[0].footer.text or ""
            match = re.search(r"POST #(\d+)", footer)
            post_id = int(match.group(1)) if match else None
        post = self.bot.database.get_code(post_id) if post_id else None
        code = post["code"] if post else None
        if code is None and interaction.message and interaction.message.embeds:
            for field in interaction.message.embeds[0].fields:
                if field.name == "코드":
                    match = re.fullmatch(r"`([A-Z0-9]{8})`", field.value)
                    code = match.group(1) if match else None
                    break
        if not code:
            await interaction.response.send_message("이 버튼의 코드 기록을 찾을 수 없습니다.", ephemeral=True)
            return
        await interaction.response.send_message(f"```\n{code}\n```", ephemeral=True)

    @discord.ui.button(label="지난 코드", style=discord.ButtonStyle.secondary, custom_id="shuffle:history")
    async def show_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        rows = self.bot.database.latest_codes(10)
        embed = discord.Embed(title="최근 보너스 코드", color=0x95A5A6)
        embed.description = "\n\n".join(format_code_row(row) for row in rows) or "저장된 코드가 없습니다."
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="내 알림 설정", style=discord.ButtonStyle.primary, custom_id="shuffle:preferences")
    async def show_preferences(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = PreferenceView(self.bot)
        await interaction.response.send_message(view.status_text(interaction.user), view=view, ephemeral=True)


class CopyCodeButton(discord.ui.DynamicItem[discord.ui.Button], template=r"shuffle:v2:copy:(?P<post_id>\d+):(?P<code>[A-Z0-9]{8})"):
    def __init__(self, post_id: int, code: str):
        self.post_id = post_id
        self.code = code
        super().__init__(
            discord.ui.Button(
                label="코드 복사",
                emoji="📋",
                style=discord.ButtonStyle.success,
                custom_id=f"shuffle:v2:copy:{post_id}:{code}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["post_id"]), match["code"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"```\n{self.code}\n```", ephemeral=True)


class CodePanelView(discord.ui.LayoutView):
    def __init__(self, bot, post: dict, alert_role: discord.Role | None = None):
        super().__init__(timeout=None)
        self.bot = bot
        post_id = int(post.get("id", post.get("post_id", 0)))
        code = str(post["code"])
        posted_at = post.get("posted_at")
        if isinstance(posted_at, str):
            try:
                posted_at = datetime.fromisoformat(posted_at)
            except ValueError:
                posted_at = None
        if posted_at is None:
            posted_at = datetime.now(timezone.utc)
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=timezone.utc)
        timestamp = int(posted_at.timestamp())

        users = f" · 선착순 **{post['users']}명**" if post.get("users") else ""
        days = f" · 최근 **{post['days']}일**" if post.get("days") else ""
        link = post.get("link")
        link_line = f"[혜택 페이지 열기 ↗]({link})" if link else "혜택 페이지 정보가 없습니다."

        history_button = discord.ui.Button(
            label="지난 코드", emoji="🗂️", style=discord.ButtonStyle.secondary,
            custom_id="shuffle:v2:history",
        )
        history_button.callback = self.show_history
        preferences_button = discord.ui.Button(
            label="내 알림 설정", emoji="🔔", style=discord.ButtonStyle.primary,
            custom_id="shuffle:v2:preferences",
        )
        preferences_button.callback = self.show_preferences

        children = [discord.ui.TextDisplay("## 🟢 SHUFFLEBOOST  /  LIVE DROP\n`BONUS CODE · TELEGRAM`")]
        if alert_role:
            children.append(discord.ui.TextDisplay(alert_role.mention))
        children.extend(
            [
                discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
                discord.ui.Section(
                    discord.ui.TextDisplay(f"### 🎟️ 보너스 코드\n## `{code}`\n`POST #{post_id}`"),
                    accessory=CopyCodeButton(post_id, code),
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"### 💎 보너스\n**{display_amount(post.get('bonus'))}**{users}\n\n"
                    f"### 🎲 베팅 요건\n**{display_amount(post.get('wager'))}**{days}"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"### 🕒 게시 시각\n<t:{timestamp}:F>  ·  <t:{timestamp}:R>\n\n{link_line}"
                ),
                discord.ui.Separator(visible=False),
                discord.ui.ActionRow(history_button, preferences_button),
            ]
        )
        self.add_item(
            discord.ui.Container(*children, accent_color=discord.Color.from_rgb(46, 204, 113))
        )

    async def show_history(self, interaction: discord.Interaction):
        rows = self.bot.database.latest_codes(10)
        embed = discord.Embed(title="최근 보너스 코드", color=0x95A5A6)
        embed.description = "\n\n".join(format_code_row(row) for row in rows) or "저장된 코드가 없습니다."
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def show_preferences(self, interaction: discord.Interaction):
        view = PreferenceView(self.bot)
        await interaction.response.send_message(view.status_text(interaction.user), view=view, ephemeral=True)


class PreferenceView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    def status_text(self, user: discord.User, role_enabled: bool | None = None) -> str:
        preference = self.bot.database.preference(user.id)
        role_id = self.bot.database.get_setting("alert_role_id")
        if not role_id:
            role_text = "설정되지 않음"
        elif role_enabled is not None:
            role_text = "ON" if role_enabled else "OFF"
        elif isinstance(user, discord.Member):
            role_text = "ON" if any(role.id == int(role_id) for role in user.roles) else "OFF"
        else:
            role_text = "서버에서 확인 필요"
        dm_text = "ON" if preference["dm_enabled"] else "OFF"
        threshold = float(preference["min_bonus"])
        threshold_text = "전체" if threshold == 0 else f"${threshold:g} 이상"
        return f"역할 알림: **{role_text}** · DM 알림: **{dm_text}** · 최소 보너스: **{threshold_text}**"

    @discord.ui.button(label="역할 알림 ON/OFF", style=discord.ButtonStyle.secondary, custom_id="shuffle:prefs:role")
    async def toggle_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        role_id = self.bot.database.get_setting("alert_role_id")
        if not interaction.guild or not role_id:
            await interaction.response.edit_message(content="서버 또는 알림 역할이 설정되지 않았습니다.", view=self)
            return
        role = interaction.guild.get_role(int(role_id))
        if role is None:
            await interaction.response.edit_message(content="알림 역할을 찾을 수 없습니다. 관리자에게 확인해 주세요.", view=self)
            return
        member = interaction.user
        if not isinstance(member, discord.Member):
            member = await interaction.guild.fetch_member(interaction.user.id)
        role_enabled = role in member.roles
        try:
            if role_enabled:
                await member.remove_roles(role, reason="사용자 알림 설정")
                role_enabled = False
                result = "역할 알림을 껐습니다."
            else:
                await member.add_roles(role, reason="사용자 알림 설정")
                role_enabled = True
                result = "역할 알림을 켰습니다."
        except discord.Forbidden:
            result = "역할을 변경할 권한이 없습니다. 봇 역할 위치와 권한을 확인해 주세요."
        await interaction.response.edit_message(
            content=f"{result}\n{self.status_text(interaction.user, role_enabled)}",
            view=self,
        )

    @discord.ui.button(label="DM 알림 ON/OFF", style=discord.ButtonStyle.secondary, custom_id="shuffle:prefs:dm")
    async def toggle_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        preference = self.bot.database.preference(interaction.user.id)
        enabled = 0 if preference["dm_enabled"] else 1
        if enabled:
            try:
                await interaction.user.send("DM 코드 알림을 켰습니다. 새 코드가 등록되면 이곳으로 알려드릴게요.")
            except discord.Forbidden:
                await interaction.response.edit_message(
                    content="DM이 차단되어 알림을 켤 수 없습니다. 개인정보 설정에서 서버 멤버의 DM을 허용한 뒤 다시 시도해 주세요.\n"
                    + self.status_text(interaction.user),
                    view=self,
                )
                return
            except discord.HTTPException:
                await interaction.response.edit_message(
                    content="DM을 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.\n" + self.status_text(interaction.user),
                    view=self,
                )
                return
        self.bot.database.save_preference(interaction.user.id, enabled, preference["min_bonus"])
        await interaction.response.edit_message(content=self.status_text(interaction.user), view=self)

    @discord.ui.select(
        placeholder="DM 최소 보너스 금액",
        options=[
            discord.SelectOption(label="전체", value="0"),
            discord.SelectOption(label="$1 이상", value="1"),
            discord.SelectOption(label="$2 이상", value="2"),
            discord.SelectOption(label="$2.5 이상", value="2.5"),
        ],
        custom_id="shuffle:prefs:minimum",
    )
    async def select_minimum(self, interaction: discord.Interaction, select: discord.ui.Select):
        preference = self.bot.database.preference(interaction.user.id)
        self.bot.database.save_preference(
            interaction.user.id, preference["dm_enabled"], float(select.values[0])
        )
        await interaction.response.edit_message(content=self.status_text(interaction.user), view=self)


class RecordPager(discord.ui.View):
    def __init__(self, bot, rows: list, page: int, page_size: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.rows = rows
        self.page = page
        self.page_size = page_size
        self.previous.disabled = page <= 0
        self.next.disabled = (page + 1) * page_size >= len(rows)

    def make_embed(self) -> discord.Embed:
        start = self.page * self.page_size
        entries = self.rows[start:start + self.page_size]
        embed = discord.Embed(title="보너스 코드 기록", color=0x2ECC71)
        embed.description = "\n\n".join(format_code_row(row) for row in entries) or "저장된 코드가 없습니다."
        total_pages = max(1, (len(self.rows) + self.page_size - 1) // self.page_size)
        embed.set_footer(text=f"페이지 {self.page + 1}/{total_pages} · 전체 {len(self.rows)}개")
        return embed

    async def change_page(self, interaction: discord.Interaction, delta: int):
        self.page += delta
        self.previous.disabled = self.page <= 0
        self.next.disabled = (self.page + 1) * self.page_size >= len(self.rows)
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="이전", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_page(interaction, -1)

    @discord.ui.button(label="다음", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_page(interaction, 1)


class ShuffleBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.database: Database | None = None
        self.session: aiohttp.ClientSession | None = None
        self.consecutive_failures = 0
        self.current_poll_interval = POLL_INTERVAL_SECONDS
        self.last_checked: datetime | None = None
        self.paused = False
        self.startup_backfill_task: asyncio.Task | None = None
        self.dm_queue: asyncio.Queue[dict] = asyncio.Queue()
        self.dm_workers: list[asyncio.Task] = []
        self.landing_retry_at: dict[int, float] = {}
        self.landing_failures: dict[int, int] = {}
        self.forum_recovery_floor: int | None = None
        self.forum_browser_runtime = None
        self.forum_browser = None
        self.forum_browser_context = None
        self.forum_page = None
        self.forum_browser_lock = asyncio.Lock()

    async def setup_hook(self):
        self.database = Database(DATABASE_PATH)
        timeout = aiohttp.ClientTimeout(total=20)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ShuffleBoostMonitor/1.0"},
        )
        self.paused = self.database.get_setting("paused") == "1"
        self.dm_workers = [
            asyncio.create_task(self.dm_worker(), name=f"dm-worker-{worker_id}")
            for worker_id in range(3)
        ]
        self.add_view(CodeView(self))
        self.add_dynamic_items(CopyCodeButton)
        self.add_view(
            CodePanelView(
                self,
                {"id": 0, "code": "TEST2026", "posted_at": datetime.now(timezone.utc)},
            )
        )
        self.poll.change_interval(seconds=POLL_INTERVAL_SECONDS)
        self.poll.start()

    async def close(self):
        self.poll.cancel()
        if self.startup_backfill_task and not self.startup_backfill_task.done():
            self.startup_backfill_task.cancel()
            await asyncio.gather(self.startup_backfill_task, return_exceptions=True)
        if self.dm_workers:
            try:
                await asyncio.wait_for(self.dm_queue.join(), timeout=3)
            except asyncio.TimeoutError:
                log.warning("DM 큐가 종료 제한 시간 내에 비워지지 않았습니다.")
            for worker in self.dm_workers:
                worker.cancel()
            await asyncio.gather(*self.dm_workers, return_exceptions=True)
        if self.session and not self.session.closed:
            await self.session.close()
        if self.forum_browser:
            await self.forum_browser.close()
        if self.forum_browser_runtime:
            await self.forum_browser_runtime.stop()
        if self.database:
            self.database.close()
        await super().close()

    async def fetch_page(self, before: int | None = None) -> list[dict]:
        url = TELEGRAM_URL if before is None else f"{TELEGRAM_URL}?before={before}"
        async with self.session.get(url) as response:
            response.raise_for_status()
            html = await response.text()
        posts = parse_posts(html)
        if not posts:
            log.warning("Telegram 게시물을 파싱하지 못했습니다. HTML 구조 변경 가능성 확인 필요")
        return posts

    async def store_posts(self, posts: list[dict]) -> None:
        for post in posts:
            self.database.save_post(post)

    async def fetch_new_posts(
        self,
        last_post_id: int,
        before: int | None = None,
        first_page: list[dict] | None = None,
    ) -> tuple[list[dict], bool, int | None, int | None]:
        collected = {}
        posts = first_page if before is None and first_page is not None else await self.fetch_page(before)
        newest_id = posts[-1]["id"] if posts and before is None else None
        for page_number in range(POLL_CATCHUP_MAX_PAGES):
            if not posts:
                return [], True, None, newest_id
            for post in posts:
                if post["id"] > last_post_id:
                    collected[post["id"]] = post
            oldest_id = posts[0]["id"]
            if oldest_id <= last_post_id:
                return [collected[key] for key in sorted(collected)], True, None, newest_id
            if page_number + 1 >= POLL_CATCHUP_MAX_PAGES:
                return [collected[key] for key in sorted(collected)], False, oldest_id, newest_id
            older_posts = await self.fetch_page(before=oldest_id)
            if not older_posts:
                return [collected[key] for key in sorted(collected)], True, None, newest_id
            if older_posts[0]["id"] >= oldest_id:
                log.warning("catch-up pagination did not move backwards; stopping at post %s", oldest_id)
                return [collected[key] for key in sorted(collected)], False, oldest_id, newest_id
            posts = older_posts
        return [collected[key] for key in sorted(collected)], False, None, newest_id

    async def fetch_landing_code(self, url: str, posted_at: datetime | None) -> str | None:
        if not is_allowed_landing_url(url):
            return None
        async with self.forum_browser_lock:
            try:
                await self._ensure_forum_browser()
                await self.forum_page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if not is_allowed_landing_url(self.forum_page.url):
                    log.warning("커뮤니티 페이지가 허용되지 않은 주소로 이동했습니다.")
                    return None
                await self.forum_page.locator("article").first.wait_for(
                    state="attached",
                    timeout=30000,
                )
                text = await self.forum_page.locator("body").inner_text(timeout=5000)
                if len(text.encode("utf-8")) > FORUM_RESPONSE_MAX_BYTES:
                    log.warning("커뮤니티 페이지가 크기 제한을 초과했습니다.")
                    return None
                return extract_landing_page_code(text, posted_at)
            except PlaywrightTimeoutError as error:
                log.info(
                    "커뮤니티 글이 브라우저에 표시되지 않았습니다. "
                    "보안 확인이 보이면 브라우저에서 직접 완료한 뒤 재시도합니다: %s",
                    error,
                )
            except PlaywrightError as error:
                log.warning("커뮤니티 브라우저 조회 실패: %s", error)
            return None

    async def _ensure_forum_browser(self) -> None:
        if self.forum_page and not self.forum_page.is_closed():
            return
        if self.forum_browser_runtime is None:
            self.forum_browser_runtime = await async_playwright().start()
        if self.forum_browser is None or not self.forum_browser.is_connected():
            self.forum_browser = await self.forum_browser_runtime.chromium.launch(headless=False)
            self.forum_browser_context = await self.forum_browser.new_context()
        self.forum_page = await self.forum_browser_context.new_page()
        await self.forum_page.route("**/*", self._guard_forum_navigation)

    async def _guard_forum_navigation(self, route) -> None:
        request = route.request
        if (
            request.is_navigation_request()
            and request.frame == self.forum_page.main_frame
            and not is_allowed_landing_url(request.url)
        ):
            log.warning("허용되지 않은 커뮤니티 페이지 이동을 차단했습니다: %s", request.url)
            await route.abort()
            return
        await route.continue_()

    async def resolve_recent_forum_codes(self, posts: list[dict], checkpoint: int) -> None:
        recent_posts = sorted(posts, key=lambda post: post["id"])[-FORUM_CODE_RECOVERY_POSTS:]
        for post in recent_posts:
            post_id = int(post["id"])
            if post.get("code") or not is_allowed_landing_url(post.get("link")):
                continue
            if post_id <= checkpoint and (
                self.forum_recovery_floor is None or post_id < self.forum_recovery_floor
            ):
                continue
            stored = self.database.get_code(post_id)
            if stored is None:
                if time.monotonic() < self.landing_retry_at.get(post_id, 0):
                    continue
                code = await self.fetch_landing_code(post["link"], post.get("posted_at"))
                if code is None:
                    failures = self.landing_failures.get(post_id, 0) + 1
                    self.landing_failures[post_id] = failures
                    self.landing_retry_at[post_id] = time.monotonic() + min(10 * (2 ** min(failures - 1, 5)), 300)
                    continue
                resolved_post = dict(post, code=code)
                self.database.save_post(resolved_post)
                self.landing_failures.pop(post_id, None)
                self.landing_retry_at.pop(post_id, None)
                stored = self.database.get_code(post_id)
                log.info("Telegram 링크 페이지에서 코드 확인: post=%s", post_id)

            if stored and not stored["sent_at"] and post_id <= checkpoint and not self.paused:
                resolved_post = dict(stored)
                resolved_post.update(
                    id=post_id,
                    link=stored["benefit_url"],
                    url=stored["post_url"],
                )
                try:
                    await self.send_code(resolved_post)
                except (discord.HTTPException, RuntimeError) as error:
                    log.warning("복구된 post %s 전송 대기: %s", post_id, error)

    async def deliver_ready_catchup(self, last_post_id: int, through_post_id: int) -> bool:
        for row in self.database.pending_codes_after(last_post_id, through_post_id):
            if self.paused:
                continue
            post = dict(row)
            post.update(
                id=post["post_id"],
                link=post["benefit_url"],
                url=post["post_url"],
            )
            try:
                await self.send_code(post)
            except (discord.HTTPException, RuntimeError, aiohttp.ClientError) as error:
                log.error("post %s 전송 실패. 완료 대기 상태를 유지하고 재시도합니다: %s", post["id"], error)
                return False

        self.database.set_setting("last_post_id", through_post_id)
        for key in ("catchup_before_id", "catchup_latest_id", "catchup_ready_until"):
            self.database.delete_setting(key)
        return True

    async def backfill(self, max_pages: int) -> int:
        before = None
        saved = 0
        for page_number in range(max_pages):
            if page_number:
                await asyncio.sleep(1)
            try:
                posts = await self.fetch_page(before)
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                log.warning("기록 백필 %s페이지 요청 실패: %s", page_number + 1, error)
                break
            if not posts:
                break
            await self.store_posts(posts)
            saved += sum(1 for post in posts if post.get("code"))
            oldest_id = posts[0]["id"]
            if before is not None and oldest_id >= before:
                log.warning("Telegram 페이지가 이전으로 이동하지 않아 백필을 중단합니다.")
                break
            before = oldest_id
        log.info("기록 백필 완료: %s페이지 확인, 코드 %s개 저장", page_number + 1 if max_pages else 0, saved)
        return saved

    async def update_panel_message(
        self,
        channel,
        post: dict,
        role: discord.Role | None,
        nonce: str | None = None,
    ):
        message = None
        panel_id = self.database.get_setting("panel_message_id")
        if panel_id:
            try:
                message = await channel.fetch_message(int(panel_id))
            except discord.NotFound:
                self.database.delete_setting("panel_message_id")
        if message is not None and self.user and message.author.id != self.user.id:
            self.database.delete_setting("panel_message_id")
            message = None

        if message is None:
            previous_delivery = self.database.latest_sent_delivery()
            if previous_delivery:
                try:
                    candidate = await channel.fetch_message(int(previous_delivery["discord_message_id"]))
                    if self.user and candidate.author.id == self.user.id:
                        message = candidate
                        self.database.set_setting("panel_message_id", candidate.id)
                except discord.NotFound:
                    pass

        if message is None and nonce:
            try:
                async for candidate in channel.history(limit=100):
                    if (
                        self.user
                        and candidate.author.id == self.user.id
                        and str(candidate.nonce) == nonce
                    ):
                        message = candidate
                        break
            except discord.Forbidden as error:
                raise RuntimeError("고정 패널 복구에 메시지 기록 보기 권한이 필요합니다.") from error

        allowed_mentions = discord.AllowedMentions(roles=[role] if role else [], users=False, everyone=False)
        view = CodePanelView(self, post, alert_role=role)
        if message is not None:
            message = await message.edit(
                content=None,
                embeds=[],
                attachments=[],
                view=view,
                allowed_mentions=allowed_mentions,
            )
        else:
            message = await channel.send(
                view=view,
                allowed_mentions=allowed_mentions,
                nonce=nonce,
            )
        self.database.set_setting("panel_message_id", message.id)
        return message

    async def send_code(self, post: dict) -> None:
        channel_id = self.database.get_setting("drop_channel_id")
        if not channel_id:
            raise RuntimeError("드랍 채널이 설정되지 않았습니다. 관리자: !채널설정")
        channel = self.get_channel(int(channel_id))
        if channel is None:
            channel = await self.fetch_channel(int(channel_id))
        role_id = self.database.get_setting("alert_role_id")
        role = channel.guild.get_role(int(role_id)) if role_id and getattr(channel, "guild", None) else None
        post_id = int(post["id"])
        nonce = f"sb{post_id}"
        delivery = self.database.get_delivery(post_id)
        if delivery and delivery["state"] == "sent":
            self.database.mark_sent(post_id, int(delivery["discord_message_id"]))
            return

        self.database.begin_delivery(post_id, nonce)
        message = await self.update_panel_message(channel, post, role, nonce)
        self.database.mark_sent(post_id, message.id)
        await self.dm_queue.put(dict(post))
        log.info("고정 패널 갱신 완료: post=%s discord_message=%s", post_id, message.id)

    async def dm_worker(self) -> None:
        while True:
            post = await self.dm_queue.get()
            try:
                await self.send_dm_notifications(post)
            except Exception:
                log.exception("DM 알림 작업 실패: post=%s", post.get("id"))
            finally:
                self.dm_queue.task_done()

    async def send_dm_notifications(self, post: dict) -> None:
        amount = parse_amount(post.get("bonus"))
        rows = self.database.connection.execute(
            "SELECT user_id, min_bonus FROM user_preferences WHERE dm_enabled = 1"
        ).fetchall()
        for row in rows:
            if amount < float(row["min_bonus"]):
                continue
            user = self.get_user(int(row["user_id"]))
            if user is None:
                try:
                    user = await self.fetch_user(int(row["user_id"]))
                except discord.HTTPException as error:
                    log.info("DM 수신자 조회 실패 user=%s: %s", row["user_id"], error)
                    continue
            try:
                await user.send(f"새 보너스 코드: `{post['code']}`\n{post['url']}")
            except discord.Forbidden:
                log.info("DM이 차단됨 user=%s", row["user_id"])
            except discord.HTTPException as error:
                log.info("DM 전송 실패 user=%s: %s", row["user_id"], error)

    async def bootstrap(self):
        previous_checkpoint = self.database.get_setting("last_post_id")
        try:
            posts = await self.fetch_page()
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            log.warning("초기 기준점 조회 실패: %s", error)
            return
        if posts and self.database.get_setting("last_post_id") is None:
            await self.store_posts(posts)
            newest = posts[-1]["id"]
            self.database.set_setting("last_post_id", newest)
            log.info("최초 실행 기준점 설정: post=%s (기존 글은 드랍하지 않음)", newest)
        elif previous_checkpoint is not None:
            self.forum_recovery_floor = max(
                0, int(previous_checkpoint) - FORUM_CODE_RECOVERY_POSTS + 1
            )
        self.startup_backfill_task = asyncio.create_task(
            self.backfill(BACKFILL_MAX_PAGES), name="telegram-startup-backfill"
        )

    @tasks.loop(seconds=60)
    async def poll(self):
        last_id_text = self.database.get_setting("last_post_id")
        try:
            if last_id_text is None:
                posts = await self.fetch_page()
                if posts:
                    await self.store_posts(posts)
                    self.database.set_setting("last_post_id", posts[-1]["id"])
                    log.info("첫 조회 기준점 설정: post=%s", posts[-1]["id"])
                return

            last_id = int(last_id_text)
            latest_page = await self.fetch_page()
            await self.resolve_recent_forum_codes(latest_page, last_id)
            ready_text = self.database.get_setting("catchup_ready_until")
            if ready_text is not None:
                await self.deliver_ready_catchup(last_id, int(ready_text))
                return

            before_text = self.database.get_setting("catchup_before_id")
            target_text = self.database.get_setting("catchup_latest_id")
            posts, caught_up, next_before, observed_latest = await self.fetch_new_posts(
                last_id,
                int(before_text) if before_text else None,
                first_page=latest_page,
            )
            await self.store_posts(posts)
            if target_text is None and observed_latest is not None:
                target_text = str(observed_latest)
                self.database.set_setting("catchup_latest_id", observed_latest)
            if not caught_up:
                if next_before is not None:
                    self.database.set_setting("catchup_before_id", next_before)
                log.info("catch-up 진행 중: checkpoint=%s next_before=%s", last_id, next_before)
                return

            through_post_id = int(target_text) if target_text is not None else max(
                [last_id, *(post["id"] for post in posts)]
            )
            self.database.set_setting("catchup_ready_until", through_post_id)
            await self.deliver_ready_catchup(last_id, through_post_id)
            self.last_checked = datetime.now(timezone.utc)
            self.consecutive_failures = 0
            self.current_poll_interval = POLL_INTERVAL_SECONDS
            self.poll.change_interval(seconds=self.current_poll_interval)
        except (aiohttp.ClientError, asyncio.TimeoutError, discord.HTTPException) as error:
            self.consecutive_failures += 1
            retry_after = None
            if isinstance(error, aiohttp.ClientResponseError) and error.status == 429:
                retry_after = (error.headers or {}).get("Retry-After")
            backoff = calculate_backoff_seconds(self.consecutive_failures, retry_after)
            self.current_poll_interval = backoff
            self.poll.change_interval(seconds=self.current_poll_interval)
            log.warning("Telegram 조회 실패 (%s회 연속), %s초 후 재시도: %s", self.consecutive_failures, backoff, error)
            return

    @poll.before_loop
    async def before_poll(self):
        await self.wait_until_ready()
        await self.bootstrap()

    async def is_admin(self, ctx: commands.Context) -> bool:
        if ctx.author.id in ADMIN_IDS:
            return True
        return isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator

    async def require_admin(self, ctx: commands.Context) -> bool:
        if await self.is_admin(ctx):
            return True
        await ctx.reply("관리자만 사용할 수 있습니다.", mention_author=False)
        return False


bot = ShuffleBot()


@bot.event
async def on_ready():
    await bot.change_presence(
        activity=discord.Streaming(
            name=STREAM_STATUS_TEXT,
            url=STREAM_STATUS_URL,
        )
    )
    log.info("Discord 연결 완료: %s (%s)", bot.user, bot.user.id if bot.user else "unknown")


@bot.command(name="도움말")
async def help_command(ctx: commands.Context):
    if not await bot.require_admin(ctx):
        return
    await ctx.reply(
        "**ShuffleBoost 코드 봇 명령어**\n"
        "`!도움말` 명령어 안내\n"
        "`!채널설정` 현재 채널을 코드 드랍 채널로 지정\n"
        "`!알림역할 @역할` 또는 `!알림역할 생성`\n"
        "`!패널` 고정 코드 패널을 최신 저장 코드로 갱신\n"
        "`!기록 [개수]` 최근 코드 조회 (최대 50개)\n"
        "`!기록동기화 [페이지수]` Telegram 과거 기록 백필\n"
        "`!상태` 봇 상태 확인\n"
        "`!일시정지` / `!재개` 코드 드랍 제어\n"
        "`!테스트` 테스트 임베드 전송",
        mention_author=False,
    )


@bot.command(name="채널설정")
async def set_channel(ctx: commands.Context):
    if not await bot.require_admin(ctx):
        return
    bot.database.set_setting("drop_channel_id", ctx.channel.id)
    await ctx.reply(f"이 채널을 코드 드랍 채널로 설정했습니다. ({ctx.channel.mention})", mention_author=False)


@bot.command(name="패널")
async def post_latest_panel(ctx: commands.Context):
    if not await bot.require_admin(ctx):
        return
    rows = bot.database.latest_codes(1)
    if not rows:
        await ctx.reply("저장된 코드가 없습니다. `!기록동기화`로 과거 기록을 먼저 가져와 주세요.", mention_author=False)
        return
    post = dict(rows[0])
    post["id"] = post["post_id"]
    channel_id = bot.database.get_setting("drop_channel_id")
    channel = ctx.channel
    if channel_id and ctx.channel.id != int(channel_id):
        channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
    role_id = bot.database.get_setting("alert_role_id")
    role = channel.guild.get_role(int(role_id)) if role_id and getattr(channel, "guild", None) else None
    await bot.update_panel_message(channel, post, role)
    await ctx.reply(f"고정 코드 패널을 최신 코드로 갱신했습니다. ({channel.mention})", mention_author=False)


@bot.command(name="알림역할")
async def set_alert_role(ctx: commands.Context, *, role_argument: str = ""):
    if not await bot.require_admin(ctx):
        return
    argument = role_argument.strip()
    if argument == "생성":
        if not ctx.guild:
            await ctx.reply("서버에서만 사용할 수 있습니다.", mention_author=False)
            return
        role = discord.utils.get(ctx.guild.roles, name="코드알림")
        if role is None:
            try:
                role = await ctx.guild.create_role(name="코드알림", reason="코드 알림 역할 생성")
            except discord.Forbidden:
                await ctx.reply("역할을 만들 권한이 없습니다.", mention_author=False)
                return
    else:
        try:
            role = await commands.RoleConverter().convert(ctx, argument)
        except (commands.BadArgument, discord.Forbidden):
            await ctx.reply("역할을 찾을 수 없습니다. `!알림역할 @역할` 또는 `!알림역할 생성`을 사용하세요.", mention_author=False)
            return
    bot.database.set_setting("alert_role_id", role.id)
    await ctx.reply(f"알림 역할을 {role.mention}으로 설정했습니다.", mention_author=False)


@bot.command(name="기록")
async def show_records(ctx: commands.Context, count: int = 10):
    if not await bot.require_admin(ctx):
        return
    page_size = min(max(count, 1), 50)
    rows = bot.database.latest_codes(50)
    view = RecordPager(bot, rows, 0, page_size)
    await ctx.reply(embed=view.make_embed(), view=view, mention_author=False)


@bot.command(name="기록동기화")
async def sync_records(ctx: commands.Context, pages: int = BACKFILL_MAX_PAGES):
    if not await bot.require_admin(ctx):
        return
    pages = min(max(pages, 1), 100)
    await ctx.reply(f"최대 {pages}페이지의 과거 글을 확인합니다.", mention_author=False)
    saved = await bot.backfill(pages)
    await ctx.send(f"기록 동기화 완료: 코드 {saved}개를 저장했습니다. 새 코드는 드랍하지 않았습니다.")


@bot.command(name="상태")
async def show_status(ctx: commands.Context):
    if not await bot.require_admin(ctx):
        return
    last_checked = bot.last_checked.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST") if bot.last_checked else "아직 조회 전"
    last_post = bot.database.get_setting("last_post_id") or "없음"
    channel_id = bot.database.get_setting("drop_channel_id") or "미설정"
    await ctx.reply(
        f"마지막 조회: {last_checked}\n마지막 글 번호: {last_post}\n저장된 코드: {bot.database.code_count()}개\n"
        f"연속 실패: {bot.consecutive_failures}회\n일시정지: {'예' if bot.paused else '아니오'}\n"
        f"폴링 간격: {bot.current_poll_interval}초\n드랍 채널 ID: {channel_id}",
        mention_author=False,
    )


@bot.command(name="일시정지")
async def pause_drops(ctx: commands.Context):
    if not await bot.require_admin(ctx):
        return
    bot.paused = True
    bot.database.set_setting("paused", 1)
    await ctx.reply("코드 드랍을 일시정지했습니다. 기록 조회는 계속됩니다.", mention_author=False)


@bot.command(name="재개")
async def resume_drops(ctx: commands.Context):
    if not await bot.require_admin(ctx):
        return
    bot.paused = False
    bot.database.set_setting("paused", 0)
    await ctx.reply("코드 드랍을 재개했습니다.", mention_author=False)


@bot.command(name="테스트")
async def test_drop(ctx: commands.Context):
    if not await bot.require_admin(ctx):
        return
    post = {
        "id": 0,
        "code": "TEST2026",
        "bonus": "1.00",
        "users": "2000",
        "wager": "3,000",
        "days": "7",
        "posted_at": datetime.now(timezone.utc),
        "link": "https://example.com/benefits",
        "url": "https://t.me/shuffleboost/0",
    }
    await ctx.send(view=CodePanelView(bot, post))


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply("명령어 인수가 부족합니다. `!도움말`을 확인해 주세요.", mention_author=False)
        return
    if isinstance(error, commands.BadArgument):
        await ctx.reply("인수 형식이 올바르지 않습니다.", mention_author=False)
        return
    log.exception("명령어 처리 오류", exc_info=error)
    await ctx.reply("명령어 처리 중 오류가 발생했습니다.", mention_author=False)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "여기에_봇_토큰":
        raise SystemExit("DISCORD_TOKEN이 설정되지 않았습니다. .env 파일에 봇 토큰을 입력하세요.")
    try:
        bot.run(token, log_handler=None)
    finally:
        if bot.database and not bot.is_closed():
            bot.database.close()


if __name__ == "__main__":
    main()
