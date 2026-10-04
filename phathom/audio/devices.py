"""Virtual audio devices (PipeWire via the pulse compat layer) — SPEC §9.1.

Layout (see SPEC §2.2):
  Chrome output -> [sink] phathom_speaker -> .monitor -> recorder (caller track)
  TTS playback  -> [sink] phathom_voice   -> .monitor -> [source] phathom_mic -> Chrome mic
"""

import asyncio
import json
import logging
import re

log = logging.getLogger("phathom.audio")


async def _run(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode().strip(), err.decode().strip()


class AudioDevices:
    """Create/remove the virtual sinks + mic source. Idempotent setup."""

    def __init__(self, listen_live: bool = False):
        self.listen_live = listen_live
        self._module_ids: list[int] = []  # only modules THIS process loaded
        self._passthrough_ids: list[int] = []

    async def _device_exists(self, kind: str, name: str) -> bool:
        # kind: "sinks" or "sources"; `pactl list short ...` rows start with "<idx>\t<name>\t..."
        rc, out, _ = await _run("pactl", "list", "short", kind)
        if rc != 0:
            return False
        return any(re.split(r"\s+", line)[1] == name for line in out.splitlines() if line.strip())

    async def _load_module(self, *args: str) -> int | None:
        rc, out, err = await _run("pactl", "load-module", *args)
        if rc != 0:
            log.error("pactl load-module %s failed: %s", args, err)
            return None
        try:
            mod_id = int(out.strip().split()[0])
        except (ValueError, IndexError):
            log.error("could not parse module id from %r", out)
            return None
        self._module_ids.append(mod_id)
        log.info("loaded module %d: %s", mod_id, " ".join(args))
        return mod_id

    async def setup(self) -> None:
        """Idempotent: reuse devices that already exist."""
        if not await self._device_exists("sinks", "phathom_speaker"):
            await self._load_module(
                "module-null-sink",
                "sink_name=phathom_speaker",
                "sink_properties=device.description=Phathom_Speaker",
            )
        if not await self._device_exists("sinks", "phathom_voice"):
            await self._load_module(
                "module-null-sink",
                "sink_name=phathom_voice",
                "sink_properties=device.description=Phathom_Voice",
            )
        if not await self._device_exists("sources", "phathom_mic"):
            await self._load_module(
                "module-remap-source",
                "master=phathom_voice.monitor",
                "source_name=phathom_mic",
                "source_properties=device.description=Phathom_Mic",
            )
        if self.listen_live and not await self._loopback_loaded():
            await self._load_module(
                "module-loopback", "source=phathom_speaker.monitor", "latency_msec=60"
            )

    async def _loopback_loaded(self) -> bool:
        rc, out, _ = await _run("pactl", "list", "short", "modules")
        if rc != 0:
            return False
        return any(
            "module-loopback" in line and "phathom_speaker.monitor" in line
            for line in out.splitlines()
        )

    async def enable_passthrough(self) -> None:
        """Owner is on the call himself (copilot): caller audio -> his real speakers/headphones,
        his real mic -> phathom_voice -> phathom_mic -> Chrome. Loaded per call, removed after,
        so his mic never leaks into calls the bot handles."""
        if self._passthrough_ids:
            return
        _, src, _ = await _run("pactl", "get-default-source")
        if not src or src.startswith("phathom_") or src.endswith(".monitor"):
            log.error("default source %r can't be used as the owner's mic; caller won't hear him", src)
        else:
            mid = await self._load_module("module-loopback", f"source={src}", "sink=phathom_voice",
                                          "latency_msec=30", "source_dont_move=true", "sink_dont_move=true")
            if mid is not None:
                self._passthrough_ids.append(mid)
        if not self.listen_live:  # LISTEN_LIVE already plays the caller on the speakers
            mid = await self._load_module("module-loopback", "source=phathom_speaker.monitor",
                                          "latency_msec=30", "source_dont_move=true")
            if mid is not None:
                self._passthrough_ids.append(mid)

    async def disable_passthrough(self) -> None:
        for mod_id in reversed(self._passthrough_ids):
            await _run("pactl", "unload-module", str(mod_id))
            if mod_id in self._module_ids:
                self._module_ids.remove(mod_id)
        if self._passthrough_ids:
            log.info("passthrough off")
        self._passthrough_ids.clear()

    async def teardown(self) -> None:
        """Unload only the module ids THIS process loaded (reverse order: dependents first)."""
        for mod_id in reversed(self._module_ids):
            rc, _, err = await _run("pactl", "unload-module", str(mod_id))
            if rc != 0:
                log.warning("unload-module %d failed: %s", mod_id, err)
            else:
                log.info("unloaded module %d", mod_id)
        self._module_ids.clear()

    async def route_chrome(self) -> bool:
        """Fallback routing: move Chrome's streams onto our devices. True if any moved."""
        moved = False
        rc, out, err = await _run("pactl", "-f", "json", "list", "sink-inputs")
        if rc == 0:
            try:
                for item in json.loads(out or "[]"):
                    props = item.get("properties", {}) or {}
                    app = str(props.get("application.name", ""))
                    if "Chrome" in app or "Chromium" in app:
                        idx = item.get("index")
                        rc2, _, err2 = await _run("pactl", "move-sink-input", str(idx), "phathom_speaker")
                        log.info("move-sink-input %s -> phathom_speaker rc=%d %s", idx, rc2, err2)
                        moved = moved or rc2 == 0
            except (ValueError, KeyError, TypeError) as e:
                log.warning("could not parse sink-inputs: %r", e)
        else:
            log.warning("list sink-inputs failed: %s", err)
        rc, out, err = await _run("pactl", "-f", "json", "list", "source-outputs")
        if rc == 0:
            try:
                for item in json.loads(out or "[]"):
                    props = item.get("properties", {}) or {}
                    app = str(props.get("application.name", ""))
                    if "Chrome" in app or "Chromium" in app:
                        idx = item.get("index")
                        rc2, _, err2 = await _run("pactl", "move-source-output", str(idx), "phathom_mic")
                        log.info("move-source-output %s -> phathom_mic rc=%d %s", idx, rc2, err2)
                        moved = moved or rc2 == 0
            except (ValueError, KeyError, TypeError) as e:
                log.warning("could not parse source-outputs: %r", e)
        else:
            log.warning("list source-outputs failed: %s", err)
        return moved
