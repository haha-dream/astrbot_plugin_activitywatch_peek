import asyncio
import hashlib
import json

import httpx

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_activitywatch_peek",
    "haha-dream",
    "利用 ActivityWatch 拉取你当前的焦点进程并推送，与 aw-peek 配合使用",
    "v1.0.1",
)
class ActivityWatchPeek(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._kv_lock = asyncio.Lock()

    async def initialize(self):
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._poll_loop())

    async def terminate(self):
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def _on_group_message(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            return

        enabled_groups = self._parse_enabled_groups(
            self.config.get("enabled_groups", "[]")
        )
        if group_id not in enabled_groups:
            return

        async with self._kv_lock:
            group_umo_map = await self.get_kv_data("group_umo_map", {})
            if not isinstance(group_umo_map, dict):
                group_umo_map = {}
            group_umo_map[str(group_id)] = event.unified_msg_origin
            await self.put_kv_data("group_umo_map", group_umo_map)

        if False:
            yield event.plain_result("")

    async def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"activitywatch_peek poll error: {e}")

            interval = self._get_interval_seconds()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _poll_once(self):
        remote_url = str(self.config.get("remote_url", "")).strip()
        if not remote_url:
            return

        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            resp = await client.get(remote_url)
            if resp.status_code != 200:
                logger.warning(f"activitywatch_peek fetch failed: {resp.status_code}")
                return

            try:
                payload = resp.json()
            except Exception as e:
                logger.warning(f"activitywatch_peek invalid json: {e}")
                return

        payload_hash = self._hash_payload(payload)

        async with self._kv_lock:
            last_hash = await self.get_kv_data("last_payload_hash", "")
            if last_hash == payload_hash:
                return
            await self.put_kv_data("last_payload_hash", payload_hash)

        await self._push_update(payload)

    async def _push_update(self, payload: object):
        enabled_groups = self._parse_enabled_groups(
            self.config.get("enabled_groups", "[]")
        )
        if not enabled_groups:
            return

        text = self._format_payload(payload)
        text = "\u200b" + text + "\u200b"

        async with self._kv_lock:
            group_umo_map = await self.get_kv_data("group_umo_map", {})
            if not isinstance(group_umo_map, dict):
                group_umo_map = {}

        for group_id in enabled_groups:
            umo = group_umo_map.get(str(group_id))
            if not umo:
                pushed = await self._try_push_aiocqhttp_group(
                    group_id=str(group_id), text=text
                )
                if not pushed:
                    logger.warning(
                        f"activitywatch_peek group {group_id} has no unified_msg_origin yet"
                    )
                continue

            try:
                await self.context.send_message(umo, [Comp.Plain(text)])
            except Exception as e:
                logger.warning(f"activitywatch_peek send to {group_id} failed: {e}")

    async def _try_push_aiocqhttp_group(self, group_id: str, text: str) -> bool:
        if not group_id.isdigit():
            return False

        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
        except Exception:
            return False

        if not platform:
            return False

        try:
            from astrbot.api.event import MessageChain
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
        except Exception:
            return False

        try:
            bot = platform.get_client()
        except Exception:
            return False

        chain = MessageChain()
        try:
            chain = chain.message(text)
        except Exception:
            try:
                chain.chain = [Comp.Plain(text)]
            except Exception:
                return False

        try:
            await AiocqhttpMessageEvent.send_message(
                bot=bot,
                message_chain=chain,
                event=None,
                is_group=True,
                session_id=group_id,
            )
            return True
        except Exception as e:
            logger.warning(f"activitywatch_peek aiocqhttp push failed: {e}")
            return False

    def _get_interval_seconds(self) -> float:
        try:
            interval = float(self.config.get("interval_seconds", 60))
        except Exception:
            interval = 60.0
        if interval <= 0:
            interval = 60.0
        return interval

    @staticmethod
    def _parse_enabled_groups(value: object) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, list):
            return {str(x).strip() for x in value if str(x).strip()}

        raw = str(value).strip()
        if not raw:
            return set()

        try:
            obj = json.loads(raw)
            if isinstance(obj, list):
                return {str(x).strip() for x in obj if str(x).strip()}
        except Exception:
            pass

        parts = []
        for line in raw.replace(",", "\n").splitlines():
            s = line.strip()
            if s:
                parts.append(s)
        return set(parts)

    @staticmethod
    def _hash_payload(payload: object) -> str:
        payload_str = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

    @staticmethod
    def _format_payload(payload: object) -> str:
        if isinstance(payload, dict):
            app = payload.get("app")
            title = payload.get("title")
            if app is not None and title is not None:
                return f"姐姐开始在 {app} 看 {title} 了"

        try:
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception:
            return str(payload)
