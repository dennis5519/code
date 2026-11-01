import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import discord

from bot import (
    BACKFILL_MAX_PAGES,
    CodePanelView,
    CodeView,
    Database,
    POLL_INTERVAL_SECONDS,
    ShuffleBot,
    bot as discord_bot,
    calculate_backoff_seconds,
    on_ready as bot_on_ready,
    on_app_command_error,
    parse_posts,
    post_latest_panel,
)


SAMPLE_HTML = """
<div class="tgme_widget_message" data-post="shuffleboost/12345">
  <div class="tgme_widget_message_text">
    Weekly bonus<br>
    AB12CD34<br>
    $1.00 for the first 2,000 users! - $3,000 wager requirement past 7 days.
    <a href="https://t.me/another_channel">Telegram link</a>
    <a href="https://shuffle.com/bonus">Claim bonus</a>
  </div>
  <a class="tgme_widget_message_date" href="https://t.me/shuffleboost/12345">
    <time datetime="2026-10-29T17:05:00+00:00"></time>
  </a>
</div>
<div class="tgme_widget_message" data-post="shuffleboost/12346">
  <div class="tgme_widget_message_text">A normal post without a code.</div>
  <a class="tgme_widget_message_date"><time datetime="2026-10-29T17:06:00Z"></time></a>
</div>
"""


class ParsePostsTests(unittest.TestCase):
    def test_extracts_code_bonus_wager_timestamp_and_external_link(self):
        posts = parse_posts(SAMPLE_HTML)
        self.assertEqual(len(posts), 2)
        post = posts[0]
        self.assertEqual(post["id"], 12345)
        self.assertEqual(post["code"], "AB12CD34")
        self.assertEqual(post["bonus"], "1.00")
        self.assertEqual(post["users"], "2,000")
        self.assertEqual(post["wager"], "3,000")
        self.assertEqual(post["days"], "7")
        self.assertEqual(post["link"], "https://shuffle.com/bonus")
        self.assertEqual(post["posted_at"], datetime(2026, 10, 29, 17, 5, tzinfo=timezone.utc))
        self.assertIsNone(posts[1]["code"])

    def test_code_requires_eight_uppercase_alphanumeric_with_letter_and_digit(self):
        html = SAMPLE_HTML.replace("AB12CD34", "ABCDEFGH")
        self.assertIsNone(parse_posts(html)[0]["code"])

    def test_ambiguous_multiple_codes_are_not_selected(self):
        html = SAMPLE_HTML.replace("AB12CD34", "AB12CD34<br>CD34EF56")
        self.assertIsNone(parse_posts(html)[0]["code"])

class PresenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_sets_custom_streaming_presence(self):
        with patch("bot.bot.change_presence", new_callable=AsyncMock) as change_presence:
            await bot_on_ready()

        change_presence.assert_awaited_once()
        activity = change_presence.await_args.kwargs["activity"]
        self.assertIsInstance(activity, discord.Streaming)
        self.assertEqual(activity.name, "> yubin12 코드로 방송중 <")
        self.assertEqual(activity.url, "https://www.twitch.tv/yubin12")


class SlashCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_all_commands_are_registered_in_the_application_command_tree(self):
        names = {command.name for command in discord_bot.tree.get_commands()}

        self.assertEqual(
            names,
            {
                "도움말",
                "채널설정",
                "패널",
                "알림역할",
                "기록",
                "기록동기화",
                "상태",
                "일시정지",
                "재개",
                "테스트",
            },
        )
        self.assertEqual(discord_bot.commands, set())

    def test_role_command_exposes_role_and_create_options(self):
        command = discord_bot.tree.get_command("알림역할")

        self.assertEqual(
            {parameter.name for parameter in command.parameters},
            {"role", "create"},
        )

    async def test_panel_command_defers_before_updating_the_panel(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            guild=SimpleNamespace(get_role=Mock(return_value=None)),
            channel=SimpleNamespace(id=12, mention="#codes", guild=SimpleNamespace()),
        )
        database = SimpleNamespace(
            latest_codes=Mock(
                return_value=[
                    {
                        "post_id": 88,
                        "code": "AB12CD34",
                        "posted_at": None,
                    }
                ]
            ),
            get_setting=Mock(return_value=None),
        )

        with patch("bot.bot.require_admin", new_callable=AsyncMock, return_value=True):
            with patch("bot.bot.database", database):
                with patch("bot.bot.update_panel_message", new_callable=AsyncMock) as update_panel:
                    async def assert_deferred(*args):
                        interaction.response.defer.assert_awaited_once_with(ephemeral=True)

                    update_panel.side_effect = assert_deferred
                    await post_latest_panel.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once()

    async def test_history_buttons_defer_before_database_lookup(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        database = SimpleNamespace(
            latest_codes=Mock(
                side_effect=lambda count: (
                    self.assertEqual(interaction.response.defer.await_count, 1),
                    [],
                )[1]
            )
        )
        client = SimpleNamespace(database=database)
        panel_view = CodePanelView(client, {"id": 1, "code": "AB12CD34"})
        legacy_view = CodeView(client)

        await panel_view.show_history(interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        interaction.followup.send.assert_awaited_once()

        interaction.response.defer.reset_mock()
        interaction.followup.send.reset_mock()
        await legacy_view.show_history.callback(interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        interaction.followup.send.assert_awaited_once()

    async def test_preferences_button_defers_before_database_lookup(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            user=SimpleNamespace(id=42),
        )
        database = SimpleNamespace(
            preference=Mock(
                side_effect=lambda user_id: (
                    self.assertEqual(interaction.response.defer.await_count, 1),
                    {"dm_enabled": False, "min_bonus": 0},
                )[1]
            ),
            get_setting=Mock(return_value=None),
        )
        panel_view = CodePanelView(SimpleNamespace(database=database), {"id": 1, "code": "AB12CD34"})

        await panel_view.show_preferences(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        interaction.followup.send.assert_awaited_once()

    async def test_interaction_expiry_during_error_reply_does_not_raise(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(is_done=Mock(return_value=True)),
            followup=SimpleNamespace(
                send=AsyncMock(
                    side_effect=discord.NotFound(
                        Mock(),
                        {"code": 10062, "message": "Unknown interaction"},
                    )
                )
            ),
        )

        await on_app_command_error(
            interaction,
            discord.app_commands.AppCommandError("command failed"),
        )

        interaction.followup.send.assert_awaited_once()


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_sets_latest_checkpoint_before_background_backfill(self):
        client = ShuffleBot()
        client.database = Database(Path(":memory:"))
        client.fetch_page = AsyncMock(return_value=[{"id": 88}])
        client.backfill = AsyncMock(return_value=0)

        await client.bootstrap()

        self.assertEqual(client.database.get_setting("last_post_id"), "88")
        client.backfill.assert_not_awaited()
        await client.startup_backfill_task
        client.backfill.assert_awaited_once_with(BACKFILL_MAX_PAGES)
        client.database.close()

    async def test_catchup_paginates_until_checkpoint_and_orders_posts(self):
        client = ShuffleBot()
        calls = []

        async def fake_fetch_page(before=None):
            calls.append(before)
            if before is None:
                return [{"id": post_id} for post_id in range(31, 51)]
            if before == 31:
                return [{"id": post_id} for post_id in range(11, 31)]
            return []

        client.fetch_page = fake_fetch_page
        posts, caught_up, next_before, newest_id = await client.fetch_new_posts(25)

        self.assertEqual([post["id"] for post in posts], list(range(26, 51)))
        self.assertEqual(calls, [None, 31])
        self.assertTrue(caught_up)
        self.assertIsNone(next_before)
        self.assertEqual(newest_id, 50)

    async def test_catchup_page_limit_returns_resume_cursor_without_completion(self):
        client = ShuffleBot()
        calls = []

        async def fake_fetch_page(before=None):
            calls.append(before)
            if before is None:
                return [{"id": post_id} for post_id in range(101, 121)]
            return [{"id": post_id} for post_id in range(81, 101)]

        client.fetch_page = fake_fetch_page
        with patch("bot.POLL_CATCHUP_MAX_PAGES", 2):
            posts, caught_up, next_before, newest_id = await client.fetch_new_posts(25)

        self.assertEqual(calls, [None, 101])
        self.assertFalse(caught_up)
        self.assertEqual(next_before, 81)
        self.assertEqual(newest_id, 120)
        self.assertEqual(posts[0]["id"], 81)

    async def test_poll_keeps_checkpoint_when_catchup_is_incomplete(self):
        client = ShuffleBot()
        client.database = Database(Path(":memory:"))
        client.database.set_setting("last_post_id", 25)
        client.fetch_page = AsyncMock(return_value=[])
        client.fetch_new_posts = AsyncMock(return_value=([], False, 81, 120))
        client.store_posts = AsyncMock()

        await client.poll()

        self.assertEqual(client.database.get_setting("last_post_id"), "25")
        self.assertEqual(client.database.get_setting("catchup_before_id"), "81")
        self.assertEqual(client.database.get_setting("catchup_latest_id"), "120")
        client.database.close()

    async def test_failed_ready_delivery_does_not_advance_checkpoint(self):
        client = ShuffleBot()
        client.database = Database(Path(":memory:"))
        client.database.set_setting("last_post_id", 25)
        client.database.set_setting("catchup_ready_until", 27)
        for post_id in (26, 27):
            client.database.save_post(
                {
                    "id": post_id,
                    "code": f"AB12CD{post_id}",
                    "url": f"https://t.me/shuffleboost/{post_id}",
                    "posted_at": None,
                }
            )
        client.send_code = AsyncMock(side_effect=RuntimeError("temporary failure"))

        completed = await client.deliver_ready_catchup(25, 27)

        self.assertFalse(completed)
        self.assertEqual(client.database.get_setting("last_post_id"), "25")
        self.assertEqual(client.database.get_setting("catchup_ready_until"), "27")
        client.send_code.assert_awaited_once()
        client.database.close()

    async def test_dm_worker_consumes_queue_without_blocking_calling_task(self):
        client = ShuffleBot()
        client.send_dm_notifications = AsyncMock()
        worker = __import__("asyncio").create_task(client.dm_worker())
        await client.dm_queue.put({"id": 88, "code": "AB12CD34"})
        await __import__("asyncio").wait_for(client.dm_queue.join(), timeout=1)
        worker.cancel()
        await __import__("asyncio").gather(worker, return_exceptions=True)
        client.send_dm_notifications.assert_awaited_once()

    async def test_outbox_records_message_id_and_sent_state(self):
        database = Database(Path(":memory:"))
        database.save_post(
            {"id": 7, "code": "AB12CD34", "url": "https://t.me/shuffleboost/7", "posted_at": None}
        )
        database.begin_delivery(7, "sb7")
        self.assertEqual(database.get_delivery(7)["state"], "sending")
        database.mark_sent(7, 123456)
        delivery = database.get_delivery(7)
        self.assertEqual(delivery["state"], "sent")
        self.assertEqual(delivery["discord_message_id"], 123456)
        self.assertIsNotNone(database.get_code(7)["sent_at"])
        database.close()

    def test_backoff_respects_retry_after_without_exceeding_cap(self):
        self.assertGreaterEqual(calculate_backoff_seconds(1, "45"), 45)
        self.assertEqual(calculate_backoff_seconds(8, "1800"), 900)
        self.assertEqual(calculate_backoff_seconds(1, "invalid"), POLL_INTERVAL_SECONDS * 2)

    def test_backoff_accepts_http_date_retry_after(self):
        retry_time = datetime.now(timezone.utc) + timedelta(seconds=45)
        retry_after = format_datetime(retry_time, usegmt=True)
        self.assertGreaterEqual(calculate_backoff_seconds(1, retry_after), 44)


class FixedPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_updates_saved_panel_without_sending_new_message(self):
        client = ShuffleBot()
        client.database = Database(Path(":memory:"))
        client.database.set_setting("panel_message_id", 321)
        message = SimpleNamespace(
            id=321,
            author=SimpleNamespace(id=1),
        )
        message.edit = AsyncMock(return_value=message)
        channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=message),
            send=AsyncMock(),
        )
        post = {
            "id": 88,
            "code": "AB12CD34",
            "posted_at": datetime(2026, 10, 30, tzinfo=timezone.utc),
            "bonus": "2.00",
            "users": "1000",
            "wager": "7,000",
            "days": "7",
        }

        with patch.object(ShuffleBot, "user", new_callable=PropertyMock, return_value=SimpleNamespace(id=1)):
            await client.update_panel_message(channel, post, role=None)

        channel.fetch_message.assert_awaited_once_with(321)
        message.edit.assert_awaited_once()
        channel.send.assert_not_awaited()
        self.assertEqual(client.database.get_setting("panel_message_id"), "321")
        client.database.close()

    async def test_adopts_latest_sent_bot_message_when_panel_id_is_missing(self):
        client = ShuffleBot()
        client.database = Database(Path(":memory:"))
        client.database.save_post(
            {"id": 87, "code": "ZX12CV34", "url": "https://t.me/shuffleboost/87", "posted_at": None}
        )
        client.database.begin_delivery(87, "sb87")
        client.database.mark_sent(87, 654)
        message = SimpleNamespace(
            id=654,
            author=SimpleNamespace(id=1),
        )
        message.edit = AsyncMock(return_value=message)
        channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=message),
            send=AsyncMock(),
        )
        post = {
            "id": 88,
            "code": "AB12CD34",
            "posted_at": datetime(2026, 10, 30, tzinfo=timezone.utc),
        }

        with patch.object(ShuffleBot, "user", new_callable=PropertyMock, return_value=SimpleNamespace(id=1)):
            await client.update_panel_message(channel, post, role=None)

        channel.fetch_message.assert_awaited_once_with(654)
        message.edit.assert_awaited_once()
        channel.send.assert_not_awaited()
        self.assertEqual(client.database.get_setting("panel_message_id"), "654")
        client.database.close()


if __name__ == "__main__":
    unittest.main()