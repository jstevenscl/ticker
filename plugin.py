import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths — derived from this file's actual location on disk
# ---------------------------------------------------------------------------

_PLUGIN_DIR        = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_KEY        = os.path.basename(_PLUGIN_DIR)   # e.g. "ticker" or "ticker_0_2_00_dev"
# Always use a fixed data directory name regardless of versioned install dir
_PLUGINS_DIR       = os.path.dirname(_PLUGIN_DIR)
_DATA_DIR          = os.path.join(_PLUGINS_DIR, "ticker_data")
TICKER_DIR         = os.path.join(_DATA_DIR, "tickers")
MAPPINGS_FILE      = os.path.join(_DATA_DIR, "mappings.json")
CHANNEL_CACHE_FILE = os.path.join(_DATA_DIR, "channel_cache.json")

# ---------------------------------------------------------------------------
# FFmpeg / StreamProfile helpers
# ---------------------------------------------------------------------------

PROFILE_PREFIX = "Ticker — "   # em dash

# Bridge-migration support for users upgrading from the pre-v0.5.00 "Tickarr" branding
# (full rename, not a fork — see CURRENT.md). Every Tickarr-era release used this same
# profile-name prefix and sibling data-dir name regardless of its own version number, so
# these are safe to hardcode as fixed legacy constants rather than something derived from
# the currently-installed folder name.
LEGACY_PROFILE_PREFIX = "Tickarr — "   # em dash
LEGACY_DATA_DIR_NAME  = "tickarr_data"

DRAWTEXT_FILTER_TEMPLATE = (
    "drawtext="
    "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    ":textfile={ticker_dir}/channel_{channel_id}_header.txt:reload=1"
    ":fontsize=36:fontcolor=white"
    ":x=(w-text_w)/2:y=(h/2-100)"
    ":box=1:boxcolor=black@0.85:boxborderw=5,"
    "drawtext="
    "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    ":textfile={ticker_dir}/channel_{channel_id}_artist.txt:reload=1"
    ":fontsize=56:fontcolor=#00d4ff"
    ":x=(w-text_w)/2:y=(h/2-20),"
    "drawtext="
    "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ":textfile={ticker_dir}/channel_{channel_id}_song.txt:reload=1"
    ":fontsize=48:fontcolor=white"
    ":x=(w-text_w)/2:y=(h/2+60),"
    "drawtext="
    "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ":textfile={ticker_dir}/channel_{channel_id}_channel.txt:reload=1"
    ":fontsize=32:fontcolor=#888888"
    ":x=(w-text_w)/2:y=(h/2+130)"
)

# ---------------------------------------------------------------------------
# Custom text FFmpeg filter templates (Phase 2)
# ---------------------------------------------------------------------------

_FONT_BOLD      = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Resolved once at load time — checked in priority order.
# Multi-color sports ticker requires monospace: same char count → same text_w → sync.
def _resolve_mono_font():
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
        "/usr/share/fonts/truetype/ubuntu/UbuntuMono-B.ttf",
    ):
        if os.path.exists(path):
            logger.info(f"ticker: sports ticker mono font: {path}")
            return path
    logger.warning("ticker: no monospace font found — sports ticker will use single-layer white")
    return ""

_FONT_MONO_BOLD = _resolve_mono_font()

CUSTOM_STATIC_ALWAYS = (
    "drawtext="
    "fontfile={font}"
    ":textfile={ticker_dir}/channel_{channel_id}_custom.txt:reload=1"
    ":fontsize=48:fontcolor=white"
    ":x=(w-text_w)/2:y={y_expr}"
    ":box=1:boxcolor=black@0.85:boxborderw=6"
)

CUSTOM_SCROLL_ALWAYS = (
    "drawtext="
    "fontfile={font}"
    ":textfile={ticker_dir}/channel_{channel_id}_custom.txt:reload=1"
    ":fontsize=40:fontcolor=white"
    ":x=w-mod(t*100\\,w+text_w):y={y_expr}"
    ":box=1:boxcolor=black@0.85:boxborderw=6"
)

CUSTOM_STATIC_TIMED = (
    "drawtext="
    "fontfile={font}"
    ":textfile={ticker_dir}/channel_{channel_id}_custom.txt:reload=1"
    ":fontsize=48:fontcolor=white"
    ":x=(w-text_w)/2:y={y_expr}"
    ":box=1:boxcolor=black@0.85:boxborderw=6"
    ":enable='between(mod(t,{interval_s}),0,{duration_s})'"
)

CUSTOM_SCROLL_TIMED = (
    "drawtext="
    "fontfile={font}"
    ":textfile={ticker_dir}/channel_{channel_id}_custom.txt:reload=1"
    ":fontsize=40:fontcolor=white"
    ":x=w-mod(mod(t\\,{interval_s})*100\\,w+text_w):y={y_expr}"
    ":box=1:boxcolor=black@0.85:boxborderw=6"
    ":enable='between(mod(t,{interval_s}),0,{duration_s})'"
)

# ---------------------------------------------------------------------------
# Sports ticker FFmpeg filter templates (Phase 3)
# Three synchronized monospace layers drawn in order:
#   1. scores  — white text + background box
#   2. abbrevs — team abbreviation color, no box (overlays scores layer)
#   3. labels  — sport label color, no box (overlays both)
# All three text files are always equal character count.
# Monospace font: equal char count → equal text_w → perfect scroll sync.
# ---------------------------------------------------------------------------

_SPORTS_SCORES_LAYER = (
    "drawtext="
    "fontfile={font}"
    ":textfile={ticker_dir}/channel_{channel_id}_sports_scores.txt:reload=1"
    ":fontsize={fontsize}:fontcolor=white"
    ":x=w-mod(t*100\\,w+text_w):y={y_expr}"
    ":box=1:boxcolor=black@0.85:boxborderw=6"
)
# Fallback when no monospace font — reads merged full text, single white layer
_SPORTS_SINGLE_LAYER = (
    "drawtext="
    "fontfile={font}"
    ":textfile={ticker_dir}/channel_{channel_id}_sports_full.txt:reload=1"
    ":fontsize={fontsize}:fontcolor=white"
    ":x=w-mod(t*100\\,w+text_w):y={y_expr}"
    ":box=1:boxcolor=black@0.85:boxborderw=6"
)
_SPORTS_ABBREVS_LAYER = (
    "drawtext="
    "fontfile={font}"
    ":textfile={ticker_dir}/channel_{channel_id}_sports_abbrevs.txt:reload=1"
    ":fontsize={fontsize}:fontcolor={abbrcolor}"
    ":x=w-mod(t*100\\,w+text_w):y={y_expr}"
    ":box=0"
)
_SPORTS_LABELS_LAYER = (
    "drawtext="
    "fontfile={font}"
    ":textfile={ticker_dir}/channel_{channel_id}_sports_labels.txt:reload=1"
    ":fontsize={fontsize}:fontcolor={labelcolor}"
    ":x=w-mod(t*100\\,w+text_w):y={y_expr}"
    ":box=0"
)

# ---------------------------------------------------------------------------
# EAS — Emergency Alert System overlay filter templates
# ---------------------------------------------------------------------------
# EAS Weather Alert — dedicated to jesmannstl
# A Dispatcharr community member and severe weather enthusiast
# whose passion for keeping people informed inspired this feature.
# Rest easy.
# ---------------------------------------------------------------------------
# ── Ticker Custom style (2 layers: flashing header + yellow scroll) ──────────
_EAS_TYPE_LAYER = (
    "drawtext="
    "fontfile={font}"
    ":textfile={ticker_dir}/eas_{channel_id}_type.txt:reload=1"
    ":fontsize=28:fontcolor=white"
    ":x=(w-text_w)/2:y=h-90"
    ":box=1:boxcolor=0xFF0000@0.92:boxborderw=14"
    ":enable='lt(mod(t\\,1)\\,0.5)'"
)
_EAS_AREA_LAYER = (
    "drawtext="
    "fontfile={font}"
    ":textfile={ticker_dir}/eas_{channel_id}_area.txt:reload=1"
    ":fontsize=18:fontcolor=yellow@0.95"
    ":x=if(gt(text_w\\,w-40)\\,w-mod(t*60\\,w+text_w+40)\\,(w-text_w)/2)"
    ":y=h-48"
    ":box=1:boxcolor=black@0.82:boxborderw=8"
)

# ── Severity → label box color for broadcast style ────────────────────────────
_EAS_BROADCAST_COLORS = {
    "Extreme":  "0x990000",
    "Severe":   "0xCC0000",
    "Moderate": "0xCC6600",
    "Minor":    "0xCC9900",
}


def _build_eas_broadcast_filter(channel_id, label_color="0xCC0000"):
    """TV-station-style EAS: full-width bottom bar, auto-sized colored label, crawl on right."""
    return (
        # Full-width dark background bar
        f"drawbox=x=0:y=ih-52:w=iw:h=52:color=black@0.90:t=fill,"
        # Scrolling crawl — drawn first so label renders on top of it
        f"drawtext=fontfile={_FONT_BOLD}"
        f":textfile={TICKER_DIR}/eas_{channel_id}_area.txt:reload=1"
        f":fontsize=20:fontcolor=white"
        f":x=w-mod(t*75\\,w+text_w+20):y=h-36,"
        # Alert type label — boxborderw=18 fills the full 52px bar height,
        # x=18 puts the box left edge flush with the screen (18-18=0)
        f"drawtext=fontfile={_FONT_BOLD}"
        f":textfile={TICKER_DIR}/eas_{channel_id}_type.txt:reload=1"
        f":fontsize=16:fontcolor=white"
        f":x=18:y=h-34"
        f":box=1:boxcolor={label_color}:boxborderw=18"
    )


def _inject_drawtext(params, drawtext_filter):
    is_audio_only = "-vn" in params or (
        ("-c:a" in params or "-acodec" in params)
        and "-c:v" not in params
        and "-vcodec" not in params
    )

    if is_audio_only:
        # Remove -vn and existing -map directives (replaced below)
        params = re.sub(r"\s*-vn\b", "", params)
        params = re.sub(r"\s*-map\s+\S+", "", params)
        # Add lavfi black background as second input.
        # If -i is present in params (self-contained profiles), insert after it.
        # Otherwise Dispatcharr supplies input 0 externally — prepend lavfi so it
        # becomes input 1 after Dispatcharr's stream URL.
        lavfi = '-f lavfi -i "color=c=black:s=1280x720:r=15"'
        new_params = re.sub(r"(-i\s+\S+)", rf"\1 {lavfi}", params, count=1)
        if new_params == params:
            params = f"{lavfi} {params}"
        else:
            params = new_params
        _fc_graph = f'[1:v]{drawtext_filter}[vout]'
        fc = (
            f'-filter_complex "{_fc_graph}"'
            f' -map "[vout]" -map 0:a:0'
            f' -c:v libx264 -preset ultrafast -tune stillimage -crf 28'
        )
        if "-f mpegts" in params:
            params = params.replace("-f mpegts", f"{fc} -f mpegts")
        elif "pipe:1" in params:
            params = params.replace("pipe:1", f"{fc} pipe:1")
        else:
            params = f"{params} {fc}"
        return params

    # Replace any stream-copy video flag — FFmpeg rejects filters with stream copy.
    # zerolatency removes encoder lookahead/B-frames, so -c:a copy stays in sync
    _VID_ENCODE = "-c:v libx264 -preset ultrafast -tune zerolatency -c:a copy"
    if "-c:v copy" in params:
        params = params.replace("-c:v copy", _VID_ENCODE)
    elif "-vcodec copy" in params:
        params = params.replace("-vcodec copy", _VID_ENCODE)
    # "-c copy" copies ALL streams
    params = re.sub(r'(?<![:\w])-c\s+copy\b', _VID_ENCODE, params)

    # Deduplicate -c:a copy that arises when base profile already has it and _VID_ENCODE adds another.
    params = re.sub(r'(\s+-c:a\s+copy){2,}', ' -c:a copy', params)

    # Strip -force_key_frames. In stream-copy profiles this is ignored, but once libx264
    # is active the expression expr:gte(t,n_forced*0) evaluates true on every single frame,
    # forcing all-I-frame output — output bitrate explodes and the encoder can't keep up.
    params = re.sub(r'\s*-force_key_frames\s+"[^"]*"', '', params)
    params = re.sub(r'\s*-force_key_frames\s+\S+', '', params)

    vf_clause = f'-vf "{drawtext_filter}"'

    if "-vf " in params:
        # Prepend drawtext to existing -vf, handling both quoted and unquoted forms
        params = re.sub(r'-vf\s+"([^"]*)"', rf'-vf "{drawtext_filter},\1"', params, count=1)
        if "-vf " in params and f'"{drawtext_filter},' not in params:
            params = re.sub(r'-vf\s+(\S+)', rf'-vf "{drawtext_filter},\1"', params, count=1)
    elif "-f mpegts" in params:
        params = params.replace("-f mpegts", f"{vf_clause} -f mpegts")
    elif "pipe:1" in params:
        params = params.replace("pipe:1", f"{vf_clause} pipe:1")
    else:
        params = params + f" {vf_clause}"

    # Suppress the default 1-second muxer interleave buffer. Without this, FFmpeg
    # buffers up to 1 second of packets to interleave transcoded video against
    # pass-through audio, producing visible startup lag on stream-copy base profiles.
    if "-max_interleave_delta" not in params:
        if "-f mpegts" in params:
            params = params.replace("-f mpegts", "-max_interleave_delta 1 -f mpegts")
        elif "pipe:1" in params:
            params = params.replace("pipe:1", "-max_interleave_delta 1 pipe:1")

    return params




# Flags that must never appear in a Ticker-cloned profile.
# These cause audio gaps or stream instability on burst-delivered streams (e.g. SiriusXM).
# Only the cloned profile is modified — the original base profile is never touched.
_DANGEROUS_FLAGS = {
    "nobuffer":  "+nobuffer in -fflags causes audio gaps on burst-delivered streams (e.g. SiriusXM via best-streams.tv). FFmpeg passes burst gaps directly to the client with no internal buffering.",
    "low_delay": "-flags low_delay disables decoder delay compensation, causing the same burst-gap disconnects.",
}


def _strip_dangerous_flags(channel_name, params):
    """Strip known problematic FFmpeg flags from cloned profile parameters.
    Logs a clear notification for every flag removed.
    The original base profile is never modified — only the Ticker clone is cleaned.
    """
    removed = []

    # Strip +nobuffer from -fflags value (e.g. -fflags +discardcorrupt+nobuffer)
    if "nobuffer" in params:
        def _remove_nobuffer(m):
            value = re.sub(r'\+?nobuffer', '', m.group(2))
            value = re.sub(r'\++', '+', value).strip('+')
            if not value:
                return ''
            return m.group(1) + value
        new_params = re.sub(r'(-fflags\s+)(\S+)', _remove_nobuffer, params)
        if new_params != params:
            removed.append("+nobuffer")
            params = new_params

    # Strip -flags low_delay
    if "low_delay" in params:
        new_params = re.sub(r'\s*-flags\s+low_delay\b', '', params)
        if new_params != params:
            removed.append("-flags low_delay")
            params = new_params

    for flag in removed:
        key = flag.lstrip('+-').split()[0]
        reason = _DANGEROUS_FLAGS.get(key, "known to cause stream issues")
        logger.warning(
            f"[Ticker] Auto-removed {flag} from cloned profile for \"{channel_name}\" "
            f"— {reason} "
            f"Your original base profile is unchanged."
        )

    return params, removed


# Cached tri-state: None = not yet checked this process.
_CHANNEL_ID_TOKEN_SUPPORTED = None


def _supports_channel_id_token():
    """Detect whether this Dispatcharr install's StreamProfile.build_command() accepts
    a channel_id argument — added for the {channelId} substitution token (Dispatcharr
    issue #1252, shipped v0.29.0). Checked once per process and cached: Dispatcharr's
    version can't change without a restart, which reloads Ticker's plugin code anyway.
    Fails closed (False) on any inspection error so an ambiguous result never risks
    breaking the existing per-channel-clone behavior.
    """
    global _CHANNEL_ID_TOKEN_SUPPORTED
    if _CHANNEL_ID_TOKEN_SUPPORTED is not None:
        return _CHANNEL_ID_TOKEN_SUPPORTED
    try:
        from core.models import StreamProfile
        sig = inspect.signature(StreamProfile.build_command)
        _CHANNEL_ID_TOKEN_SUPPORTED = "channel_id" in sig.parameters
    except Exception as e:
        logger.warning(f"ticker: could not determine {{channelId}} token support, assuming unsupported — {e}")
        _CHANNEL_ID_TOKEN_SUPPORTED = False
    return _CHANNEL_ID_TOKEN_SUPPORTED


def _config_sig(*values):
    """Short deterministic signature for a tuple of filter-affecting config values —
    used as the dedup key for shared stream profiles (see _get_or_create_shared_profile).
    """
    raw = "|".join("" if v is None else str(v) for v in values)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _get_or_create_shared_profile(overlay_type, original_profile, channel_name, config_sig, build_filter_fn, post_process_fn=None):
    """Get-or-create ONE StreamProfile shared by every channel with the same
    (overlay_type, base/original profile, filter-affecting settings) — only used when
    _supports_channel_id_token() is True. build_filter_fn receives the string "{channelId}"
    (Dispatcharr's runtime substitution token, resolved per-channel at stream-start) in
    place of a real numeric channel id, and must return the drawtext/filter_complex string
    with it spliced in verbatim.

    Looked up by name (name encodes overlay_type + config_sig) rather than tracked in a
    separate index — self-healing if a profile is manually deleted, and naturally dedupes
    across every enable/on-demand-connect/test call site without any of them needing to
    know whether a matching profile already exists.

    Never deleted by a single channel's disable/idle-restore — see _delete_cloned_profile,
    which recognizes the "[shared:" name marker and only ever removes shared profiles via
    the orphan-cleanup action once no mapping references them anymore.
    """
    from core.models import StreamProfile
    name = f"{PROFILE_PREFIX}{original_profile.name} [shared:{overlay_type}:{config_sig}]"
    raw_params = original_profile.parameters or ""
    cleaned_params, removed_flags = _strip_dangerous_flags(channel_name or overlay_type, raw_params)
    drawtext = build_filter_fn("{channelId}")
    params = _inject_drawtext(cleaned_params, drawtext)
    if post_process_fn:
        params = post_process_fn(params)
    existing = StreamProfile.objects.filter(name=name).first()
    if existing:
        if existing.parameters != params:
            existing.parameters = params
            existing.save(update_fields=["parameters"])
            logger.info(f"ticker: shared profile {existing.id} ({name}) refreshed")
        return existing, removed_flags
    profile = StreamProfile(
        name=name,
        command=original_profile.command,
        parameters=params,
        locked=False,
        is_active=True,
    )
    profile.save()
    logger.info(f"ticker: shared profile created — {name} (id={profile.id})"
                + (f" (removed: {', '.join(removed_flags)})" if removed_flags else ""))
    return profile, removed_flags


def _clone_and_inject(channel_id, original_profile, channel_name=""):
    if _supports_channel_id_token():
        sig = _config_sig(original_profile.id)
        return _get_or_create_shared_profile(
            "nowplaying", original_profile, channel_name, sig,
            lambda cid_token: DRAWTEXT_FILTER_TEMPLATE.format(ticker_dir=TICKER_DIR, channel_id=cid_token),
        )
    from core.models import StreamProfile
    raw_params = original_profile.parameters or ""
    cleaned_params, removed_flags = _strip_dangerous_flags(
        channel_name or f"channel {channel_id}", raw_params
    )
    drawtext = DRAWTEXT_FILTER_TEMPLATE.format(ticker_dir=TICKER_DIR, channel_id=channel_id)
    params = _inject_drawtext(cleaned_params, drawtext)
    profile = StreamProfile(
        name=f"{PROFILE_PREFIX}{original_profile.name} [ch{channel_id}]",
        command=original_profile.command,
        parameters=params,
        locked=False,
        is_active=True,
    )
    profile.save()
    logger.info(f"ticker: cloned profile {original_profile.id} → {profile.id} for channel {channel_id}"
                + (f" (removed: {', '.join(removed_flags)})" if removed_flags else ""))
    return profile, removed_flags


def _clone_and_inject_custom(channel_id, original_profile, channel_name, style, position, schedule, duration, interval):
    if _supports_channel_id_token():
        sig = _config_sig(original_profile.id, style, position, schedule, duration, interval)
        return _get_or_create_shared_profile(
            "custom", original_profile, channel_name, sig,
            lambda cid_token: _build_custom_filter(cid_token, style, position, schedule, duration, interval),
        )
    from core.models import StreamProfile
    raw_params, removed_flags = _strip_dangerous_flags(channel_name, original_profile.parameters or "")
    drawtext = _build_custom_filter(channel_id, style, position, schedule, duration, interval)
    params = _inject_drawtext(raw_params, drawtext)
    profile = StreamProfile(
        name=f"{PROFILE_PREFIX}{original_profile.name} [ch{channel_id}]",
        command=original_profile.command,
        parameters=params,
        locked=False,
        is_active=True,
    )
    profile.save()
    logger.info(f"ticker: cloned profile {original_profile.id} → {profile.id} for channel {channel_id}"
                + (f" (removed: {', '.join(removed_flags)})" if removed_flags else ""))
    return profile, removed_flags


def _clone_and_inject_sports(channel_id, original_profile, channel_name, position, fontsize,
                             labelcolor, abbrcolor, color_mode, transcode_mode, ticker_style):
    if _supports_channel_id_token():
        sig = _config_sig(original_profile.id, position, fontsize, labelcolor, abbrcolor,
                          color_mode, transcode_mode, ticker_style)
        return _get_or_create_shared_profile(
            "sports", original_profile, channel_name, sig,
            lambda cid_token: _build_sports_filter(cid_token, position, fontsize, labelcolor,
                                                    abbrcolor, color_mode, transcode_mode, ticker_style),
        )
    from core.models import StreamProfile
    raw_params, removed_flags = _strip_dangerous_flags(channel_name, original_profile.parameters or "")
    drawtext = _build_sports_filter(channel_id, position, fontsize, labelcolor, abbrcolor,
                                    color_mode, transcode_mode, ticker_style)
    params = _inject_drawtext(raw_params, drawtext)
    profile = StreamProfile(
        name=f"{PROFILE_PREFIX}{original_profile.name} [ch{channel_id}]",
        command=original_profile.command,
        parameters=params,
        locked=False,
        is_active=True,
    )
    profile.save()
    logger.info(f"ticker: cloned profile {original_profile.id} → {profile.id} for channel {channel_id}"
                + (f" (removed: {', '.join(removed_flags)})" if removed_flags else ""))
    return profile, removed_flags


def _inject_eas_tone(params, channel_id, interval_secs=300):
    """Mix a periodic EAS attention tone (853+960 Hz) into the audio stream.

    Uses aevalsrc to generate the tone mathematically inside FFmpeg — no external
    file required, and tone repeats every interval_secs for the life of the alert.
    Each burst is 8 seconds with a smooth sin² bell-curve envelope (no clicks).
    Converts the existing -vf into a filter_complex so video overlay and audio
    mixing share one pass.
    """
    vf_match = re.search(r'-vf\s+"([^"]+)"', params)
    if not vf_match:
        logger.warning("[Ticker] EAS: no -vf in params — siren skipped")
        return params

    vf_filter = vf_match.group(1)

    # sin² bell curve over 8 s, zero outside — commas escaped for filter_complex
    # FFmpeg expressions use ^ for power, not **
    gate = f"if(lt(mod(t\\,{interval_secs})\\,8)\\,sin(3.14159*mod(t\\,{interval_secs})/8)^2\\,0)"
    expr = f"0.4*(sin(6.2832*853*t)+sin(6.2832*960*t))*{gate}"
    # Same expression for both stereo channels (| separator)
    aevalsrc = f"aevalsrc={expr}|{expr}:s=48000:c=stereo[tone]"

    fc_graph = (
        f"[0:v]{vf_filter}[vout];"
        f"{aevalsrc};"
        f"[0:a][tone]amix=inputs=2:duration=first:weights=1 0.5:normalize=0[aout]"
    )
    fc_clause = f'-filter_complex "{fc_graph}" -map "[vout]" -map "[aout]"'

    # Swap -vf for filter_complex
    params = params[:vf_match.start()] + fc_clause + params[vf_match.end():]

    # Remove bare -map 0 — filter_complex provides explicit stream maps
    params = re.sub(r'\s*-map\s+0(?!:)', '', params)

    # Audio must be re-encoded when mixing
    params = re.sub(r'\s*-c:a\s+copy\b', ' -c:a aac -b:a 192k', params)
    params = re.sub(r'\s*-acodec\s+copy\b', ' -c:a aac -b:a 192k', params)

    # Collapse any duplicate -c:a flags — can occur when the base profile has
    # both -c:v copy and -c:a copy and _inject_drawtext's _VID_ENCODE adds a
    # second -c:a copy, causing two replacements above
    params = re.sub(r'(\s+-c:a\s+aac\s+-b:a\s+192k){2,}', ' -c:a aac -b:a 192k', params)

    return params


def _inject_eas_tone_wav(params, wav_path, interval_secs=300):
    """Mix a periodic attention tone from a WAV file into the audio stream.

    Uses amovie with loop=2147483647 to stream the file continuously; a volume gate
    silences it between bursts so the tone fires once per interval_secs.
    """
    vf_match = re.search(r'-vf\s+"([^"]+)"', params)
    if not vf_match:
        logger.warning("[Ticker] EAS: no -vf in params — WAV tone skipped")
        return params

    vf_filter = vf_match.group(1)
    # FFmpeg path escaping: backslashes → forward slashes, colons escaped
    safe_path = wav_path.replace("\\", "/").replace(":", "\\:")
    # Gate: tone fires for the first 20s of each interval (extended from 8s to survive
    # stream startup buffering — by the time audio reaches the client, t may be 5-10s in)
    # Build [WAV once → silence] cycle then aloop it.
    # Avoids volume=if(...) expression which silences the tone on this FFmpeg build.
    # WAV is 8.09s; pad to full cycle duration, then loop the cycle forever.
    cycle_secs = max(10, interval_secs)
    cycle_samples = cycle_secs * 48000  # WAV is 48 kHz
    fc_graph = (
        f"[0:v]{vf_filter}[vout];"
        f"amovie={safe_path}:loop=1,apad=whole_dur={cycle_secs}[tcyc];"
        f"[tcyc]aloop=loop=-1:size={cycle_samples}[tone];"
        f"[0:a][tone]amix=inputs=2:duration=first:weights=1 1.5:normalize=0[aout]"
    )
    logger.info(f"[Ticker] EAS CA tone inject: interval={interval_secs}s cycle={cycle_secs}s")
    fc_clause = f'-filter_complex "{fc_graph}" -map "[vout]" -map "[aout]"'
    params = params[:vf_match.start()] + fc_clause + params[vf_match.end():]
    params = re.sub(r'\s*-map\s+0(?!:)', '', params)
    params = re.sub(r'\s*-c:a\s+copy\b', ' -c:a aac -b:a 192k', params)
    params = re.sub(r'\s*-acodec\s+copy\b', ' -c:a aac -b:a 192k', params)
    params = re.sub(r'(\s+-c:a\s+aac\s+-b:a\s+192k){2,}', ' -c:a aac -b:a 192k', params)
    return params


_EAS_TRANSCODE_PREFIXES = {
    # Filter prefix prepended to the EAS overlay filter chain.
    # Applied at clone time; removed automatically when the alert clears and the
    # original profile is restored.  "full" = no prefix (transcode at source quality).
    "full":    "",
    "1080p30": "fps=fps=30,",
    "720p":    "scale=1280:720:flags=fast_bilinear,",
    "720p30":  "scale=1280:720:flags=fast_bilinear,fps=fps=30,",
}


def _clone_and_inject_eas(channel_id, original_profile, channel_name="", tone_interval=0,
                          overlay_style="ticker", label_color="0xCC0000",
                          transcode_mode="full", tone_wav=None):
    if _supports_channel_id_token():
        # label_color is alert-severity-driven at runtime (only meaningful for the
        # "broadcast" style) — including it in the signature only when it's actually used
        # means channels currently showing different alert severities correctly get
        # different shared profiles, without needlessly fragmenting the "ticker" style
        # (which never reads label_color) into one profile per severity ever seen.
        sig_color = label_color if overlay_style == "broadcast" else ""
        sig = _config_sig(original_profile.id, overlay_style, transcode_mode, tone_interval,
                          sig_color, tone_wav)

        def _build(cid_token):
            transcode_prefix = _EAS_TRANSCODE_PREFIXES.get(transcode_mode, "")
            if overlay_style == "broadcast":
                return transcode_prefix + _build_eas_broadcast_filter(cid_token, label_color)
            return (
                transcode_prefix
                + _EAS_TYPE_LAYER.format(font=_FONT_BOLD, ticker_dir=TICKER_DIR, channel_id=cid_token)
                + ","
                + _EAS_AREA_LAYER.format(font=_FONT_BOLD, ticker_dir=TICKER_DIR, channel_id=cid_token)
            )

        def _post(params):
            if tone_interval <= 0:
                return params
            if tone_wav and os.path.exists(tone_wav):
                return _inject_eas_tone_wav(params, tone_wav, tone_interval)
            return _inject_eas_tone(params, "{channelId}", tone_interval)

        return _get_or_create_shared_profile(
            "eas", original_profile, channel_name, sig, _build, post_process_fn=_post,
        )
    from core.models import StreamProfile
    raw_params = original_profile.parameters or ""
    cleaned_params, removed_flags = _strip_dangerous_flags(
        channel_name or f"channel {channel_id}", raw_params
    )
    transcode_prefix = _EAS_TRANSCODE_PREFIXES.get(transcode_mode, "")
    if overlay_style == "broadcast":
        eas_filter = transcode_prefix + _build_eas_broadcast_filter(channel_id, label_color)
    else:
        eas_filter = (
            transcode_prefix
            + _EAS_TYPE_LAYER.format(font=_FONT_BOLD, ticker_dir=TICKER_DIR, channel_id=channel_id)
            + ","
            + _EAS_AREA_LAYER.format(font=_FONT_BOLD, ticker_dir=TICKER_DIR, channel_id=channel_id)
        )
    params = _inject_drawtext(cleaned_params, eas_filter)
    if tone_interval > 0:
        if tone_wav and os.path.exists(tone_wav):
            params = _inject_eas_tone_wav(params, tone_wav, tone_interval)
        else:
            params = _inject_eas_tone(params, channel_id, tone_interval)
    profile = StreamProfile(
        name=f"{PROFILE_PREFIX}EAS [{original_profile.name}] [ch{channel_id}]",
        command=original_profile.command,
        parameters=params,
        locked=False,
        is_active=True,
    )
    profile.save()
    logger.info(f"ticker: EAS profile cloned {original_profile.id} → {profile.id} for channel {channel_id}"
                + (f" (removed: {', '.join(removed_flags)})" if removed_flags else ""))
    return profile, removed_flags


def _assign_profile(channel, profile):
    channel.stream_profile = profile
    channel.save(update_fields=["stream_profile"])
    try:
        channel.update_stream_profile(profile.id)
    except Exception as e:
        logger.warning(f"ticker: update_stream_profile failed for {channel.name}: {e}")


def _assign_logo(channel, logo_url, channel_display_name):
    from apps.channels.models import Logo
    try:
        logo, created = Logo.objects.get_or_create(
            url=logo_url,
            defaults={"name": channel_display_name},
        )
        channel.logo = logo
        channel.save(update_fields=["logo"])
        return True, created
    except Exception as e:
        logger.warning(f"ticker: logo assign failed for {channel_display_name}: {e}")
        return False, False


def _restore_profile(channel, original_profile_id):
    from core.models import StreamProfile
    try:
        original = StreamProfile.objects.get(id=original_profile_id)
        _assign_profile(channel, original)
    except StreamProfile.DoesNotExist:
        channel.stream_profile = None
        channel.save(update_fields=["stream_profile"])


def _delete_cloned_profile(profile_id):
    """Delete a Ticker-managed StreamProfile — unless it's a shared profile (name contains
    "[shared:"), in which case it's retained: other channels may still be actively using it,
    and per-channel disable/idle-restore/alert-clear call sites have no way to know that.
    Shared profiles are only ever reaped by the Clean Orphaned Profiles action, once no
    mapping references them anymore.
    """
    from core.models import StreamProfile
    try:
        profile = StreamProfile.objects.filter(id=profile_id, name__startswith=PROFILE_PREFIX).first()
        if not profile:
            return
        if " [shared:" in profile.name:
            logger.debug(f"ticker: shared profile {profile_id} ({profile.name}) retained — "
                         f"may still be in use by other channels")
            return
        profile.delete()
    except Exception as e:
        logger.warning(f"ticker: could not delete profile {profile_id}: {e}")


def _get_ticker_profiles():
    from core.models import StreamProfile
    return list(StreamProfile.objects.filter(name__startswith=PROFILE_PREFIX))


def _build_custom_filter(channel_id, style, position, schedule, duration, interval):
    y_map = {
        "top":    "30",
        "center": "(h-text_h)/2",
        "bottom": "h-text_h-30",
    }
    y_expr = y_map.get(position, "h-text_h-30")

    if style == "scrolling" and schedule == "timed":
        template = CUSTOM_SCROLL_TIMED
    elif style == "scrolling":
        template = CUSTOM_SCROLL_ALWAYS
    elif schedule == "timed":
        template = CUSTOM_STATIC_TIMED
    else:
        template = CUSTOM_STATIC_ALWAYS

    return template.format(
        font=_FONT_BOLD,
        ticker_dir=TICKER_DIR,
        channel_id=channel_id,
        y_expr=y_expr,
        duration_s=int(duration),
        interval_s=int(interval) * 60,
    )

# ---------------------------------------------------------------------------
# File writer helpers
# ---------------------------------------------------------------------------


def _ensure_dirs():
    os.makedirs(TICKER_DIR, exist_ok=True)


def _atomic_write(filename, content):
    path = os.path.join(TICKER_DIR, filename)
    tmp = path + f".tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"ticker: write failed for {filename}: {e}")


def _truncate(text, max_len):
    if not text or len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def _write_nowplaying(channel_id, artist, song, channel_name):
    _ensure_dirs()
    _atomic_write(f"channel_{channel_id}_header.txt", "♫ Now Playing ♫")
    _atomic_write(f"channel_{channel_id}_artist.txt", _truncate(artist or "", 38))
    song_text = f'"{_truncate(song, 45)}"' if song else ""
    _atomic_write(f"channel_{channel_id}_song.txt", song_text)
    _atomic_write(f"channel_{channel_id}_channel.txt", channel_name or "")


def _write_custom_text(channel_id, text):
    _ensure_dirs()
    _atomic_write(f"channel_{channel_id}_custom.txt", text or "")


def _write_fallback(channel_id, name, description):
    _ensure_dirs()
    desc = (description or "")[:50] + ("..." if len(description or "") > 50 else "")
    _atomic_write(f"channel_{channel_id}_header.txt", name or "")
    _atomic_write(f"channel_{channel_id}_artist.txt", "")
    _atomic_write(f"channel_{channel_id}_song.txt", "")
    _atomic_write(f"channel_{channel_id}_channel.txt", desc)


def _remove_channel_files(channel_id):
    for suffix in ("header", "artist", "song", "channel"):
        path = os.path.join(TICKER_DIR, f"channel_{channel_id}_{suffix}.txt")
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logger.warning(f"ticker: could not remove {path}: {e}")


def _remove_custom_file(channel_id):
    path = os.path.join(TICKER_DIR, f"channel_{channel_id}_custom.txt")
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.warning(f"ticker: could not remove {path}: {e}")


def _write_sports_text(channel_id, labels_text, abbrevs_text, scores_text, full_text=""):
    _ensure_dirs()
    _atomic_write(f"channel_{channel_id}_sports_labels.txt",  labels_text  or "")
    _atomic_write(f"channel_{channel_id}_sports_abbrevs.txt", abbrevs_text or "")
    _atomic_write(f"channel_{channel_id}_sports_scores.txt",  scores_text  or "")
    _atomic_write(f"channel_{channel_id}_sports_full.txt",    full_text    or "")


def _eas_write_alert(channel_id, unique_alerts, overlay_style="ticker"):
    """Write EAS overlay text files for the active style."""
    _ensure_dirs()
    if not unique_alerts:
        return

    def _area(a):
        return (a.get("area") or "").replace("; ", "  ·  ").strip()

    if overlay_style == "broadcast":
        # Label always shows the worst active event; crawl carries all details
        type_text = unique_alerts[0]["event"].upper()
        if len(unique_alerts) == 1:
            area_text = _area(unique_alerts[0])
        else:
            parts = [f"{a['event'].upper()} — {_area(a)}" if _area(a) else a["event"].upper()
                     for a in unique_alerts]
            area_text = "     |     ".join(parts)
    else:
        # Ticker custom style — label always shows worst event
        type_text = f"⚠  {unique_alerts[0]['event'].upper()}  ⚠"
        if len(unique_alerts) == 1:
            area_text = _area(unique_alerts[0])
        else:
            parts = [f"{a['event'].upper()} — {_area(a)}" if _area(a) else a["event"].upper()
                     for a in unique_alerts]
            area_text = "     ◆     ".join(parts)

    _atomic_write(f"eas_{channel_id}_type.txt", type_text)
    _atomic_write(f"eas_{channel_id}_area.txt", area_text)
    _atomic_write(f"eas_{channel_id}_events.txt", "")


def _eas_clear(channel_id):
    _ensure_dirs()
    _atomic_write(f"eas_{channel_id}_type.txt",   "")
    _atomic_write(f"eas_{channel_id}_events.txt", "")
    _atomic_write(f"eas_{channel_id}_area.txt",   "")


_RESTART_GRACE_SECONDS = 5.0
_RESTART_ACTIVE_WAIT_SECONDS = 20.0


def _restart_channel_stream(channel_uuid, label=""):
    """Stop an active channel so clients reconnect with the updated stream profile.

    stop_channel() sets a Redis 'channel_stopping' key with a 60-second TTL.
    While that key exists, is_channel_teardown_active() returns True and Dispatcharr
    rejects ALL reconnect attempts before FFmpeg even starts — causing the 10-second
    stall loop. We delete the key after ~2s (enough for FFmpeg teardown to finish)
    so clients can reconnect immediately with the new profile.

    We wait for the channel to reach 'active' (buffer filled, client receiving data)
    before restarting — but restarting the instant it goes active is the worst
    possible timing: that's exactly when the client transitions from buffering to
    playing. So after detecting 'active' we wait _RESTART_GRACE_SECONDS more,
    giving the client real playback before pulling the stream out from under it.
    """
    import time as _time
    tag = f" [{label}]" if label else ""
    try:
        from apps.proxy.live_proxy.services.channel_service import ChannelService
        from apps.proxy.live_proxy.redis_keys import RedisKeys
        from apps.channels.models import RedisClient
        rc = RedisClient.get_client()
        meta_key = RedisKeys.channel_metadata(channel_uuid)
        deadline = _time.time() + _RESTART_ACTIVE_WAIT_SECONDS
        went_active = False
        while _time.time() < deadline:
            raw = rc.hget(meta_key, "state")
            state_val = (raw.decode() if isinstance(raw, bytes) else raw) if raw else ""
            if state_val == "active":
                went_active = True
                break
            _time.sleep(0.25)
        if went_active:
            _time.sleep(_RESTART_GRACE_SECONDS)
        result = ChannelService.stop_channel(channel_uuid)
        if result.get("status") != "success":
            return
        rc.delete(RedisKeys.channel_stopping(channel_uuid))
        meta_key = RedisKeys.channel_metadata(channel_uuid)
        state_raw = rc.hget(meta_key, "state")
        state_val = (state_raw.decode() if isinstance(state_raw, bytes) else state_raw) if state_raw else ""
        if state_val == "stopping":
            rc.hdel(meta_key, "state")
        logger.info(f"ticker:{tag} channel {channel_uuid} reconnect gate cleared")
    except ImportError:
        logger.warning(f"ticker:{tag} live_proxy unavailable — profile will apply on next client connect")
    except Exception as e:
        logger.debug(f"ticker:{tag} restart {channel_uuid}: {e}")


def _restart_channel_stream_async(channel, label=""):
    """Non-blocking wrapper — runs _restart_channel_stream in a daemon thread so the 2s sleep never blocks the caller."""
    import threading as _threading
    _threading.Thread(
        target=_restart_channel_stream,
        args=(str(channel.uuid), label),
        daemon=True,
    ).start()


def _eas_restart_channel(channel_uuid):
    _restart_channel_stream(channel_uuid, label="EAS")


def _fetch_nws_alerts(zones, severity_threshold="Moderate"):
    zone_str = ",".join(z.upper() for z in zones if z.strip())
    if not zone_str:
        return []
    url = f"{NWS_ALERTS_URL}?zone={zone_str}"
    req = urllib.request.Request(url, headers={
        "User-Agent": NWS_UA,
        "Accept": "application/geo+json",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    min_sev = _EAS_SEV.get(severity_threshold, 2)
    alerts = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        if props.get("status") != "Actual":
            continue
        if props.get("urgency") in ("Past", "Unknown"):
            continue
        if _EAS_SEV.get(props.get("severity", "Unknown"), 0) < min_sev:
            continue
        alerts.append({
            "id":       props.get("id", ""),
            "event":    props.get("event", "Weather Alert"),
            "area":     props.get("areaDesc", ""),
            "severity": props.get("severity", "Unknown"),
            "expires":  props.get("expires", ""),
        })
    return alerts


_EC_ALERTS_BASE = "https://api.weather.gc.ca/collections/citypageweather-realtime/items"
_EC_SEV_MAP = {
    "Red":    "Extreme",
    "Orange": "Severe",
    "Yellow": "Moderate",
    "Blue":   "Minor",
}


def _fetch_weather_canada_alerts(city_ids, severity_threshold="Moderate", language="en"):
    min_sev = _EAS_SEV.get(severity_threshold, 2)
    all_alerts = []

    def _loc(val):
        """Extract localised text from a bilingual EC dict based on language setting."""
        if not isinstance(val, dict):
            return val or ""
        en = (val.get("en") or "").strip()
        fr = (val.get("fr") or "").strip()
        if language == "fr":
            return fr or en
        if language == "both":
            if en and fr and en != fr:
                return f"{en} / {fr}"
            return en or fr
        return en or fr  # default: English

    for city_id in city_ids:
        city_id = city_id.strip().lower()
        if not city_id:
            continue
        url = f"{_EC_ALERTS_BASE}/{city_id}?lang=en"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": NWS_UA,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except Exception as e:
            logger.warning(f"[Ticker] EAS: Weather Canada fetch failed for {city_id}: {e}")
            continue
        props = data.get("properties", {}) or {}
        warnings = props.get("warnings") or []
        if not warnings:
            continue
        for w in warnings:
            ec_color = _loc(w.get("alertColourLevel")) or "yellow"
            severity = _EC_SEV_MAP.get(ec_color.title(), "Moderate")
            if _EAS_SEV.get(severity, 0) < min_sev:
                continue
            event = (_loc(w.get("description")) or _loc(w.get("type")) or "Weather Alert")
            area  = _loc(props.get("displayName")) or _get_ca_city_name(city_id, language)
            all_alerts.append({
                "id":       f"ec-{city_id}-{event}",
                "event":    event,
                "area":     area,
                "severity": severity,
                "expires":  _loc(w.get("expiryTime")),
                "source":   "EC",
            })
    return all_alerts


_EAS_REDIS_KEY    = f"ticker:{_PLUGIN_KEY}:eas_result"
_EAS_REDIS_LOCK   = f"ticker:{_PLUGIN_KEY}:eas_poll_lock"
_EAS_STATE_KEY    = f"ticker:{_PLUGIN_KEY}:eas_state"     # JSON {cid: event_name or null}
_EAS_OWNER_KEY    = f"ticker:{_PLUGIN_KEY}:eas_owner"     # nx lock — one worker applies transitions
_EAS_ALERTS_KEY   = f"ticker:{_PLUGIN_KEY}:eas_alerts"    # fingerprint of current alert set
_EAS_ROTATION_KEY = f"ticker:{_PLUGIN_KEY}:eas_rotation"  # current rotation index
_EAS_CACHE_TTL    = 50   # seconds — one worker polls, all others read from Redis
_EAS_CA_REDIS_KEY  = f"ticker:{_PLUGIN_KEY}:eas_ca_result"
_EAS_CA_REDIS_LOCK = f"ticker:{_PLUGIN_KEY}:eas_ca_poll_lock"
_EAS_CA_TONE_FILE      = os.path.join(_PLUGIN_DIR, "eas_tone_ca.wav")
_EAS_CA_CITY_NAMES     = os.path.join(_PLUGIN_DIR, "ca_city_names.json")

_ca_city_name_cache = None
def _get_ca_city_name(city_id, language="en"):
    global _ca_city_name_cache
    if _ca_city_name_cache is None:
        try:
            with open(_EAS_CA_CITY_NAMES, encoding="utf-8") as f:
                _ca_city_name_cache = json.load(f)
        except Exception:
            _ca_city_name_cache = {}
    names = _ca_city_name_cache.get(city_id) or {}
    en = (names.get("en") or "").strip()
    fr = (names.get("fr") or "").strip()
    if language == "fr":
        return fr or en or city_id
    if language == "both":
        if en and fr and en != fr:
            return f"{en} / {fr}"
        return en or fr or city_id
    return en or fr or city_id

def _eas_sweep():
    settings = _get_settings()
    # eas_zones key kept for backward compat; ca_city_ids is new
    zones_raw  = (settings.get("eas_zones") or "").strip()
    zones      = [z.strip() for z in zones_raw.split(",") if z.strip()]
    ca_ids_raw = (settings.get("eas_ca_city_ids") or "").strip()
    ca_ids     = [c.strip().lower() for c in ca_ids_raw.split(",") if c.strip()]
    ca_language = settings.get("eas_ca_language") or "en"

    severity_threshold = settings.get("eas_severity_filter") or "Moderate"
    try:
        tone_interval = int(settings.get("eas_tone_interval") or 300)
        tone_interval = max(30, tone_interval) if tone_interval > 0 else 0
    except Exception:
        tone_interval = 300
    overlay_style = settings.get("eas_overlay_style") or "ticker"
    transcode_mode = settings.get("eas_transcode_mode") or "full"

    mappings = _get_mappings()

    # Channels armed per source
    nws_cids = {cid for cid, m in mappings.items() if m and (
        m.get("type") == "eas" or m.get("eas_armed")
    )}
    ca_cids = {cid for cid, m in mappings.items() if m and (
        m.get("type") == "eas_ca" or m.get("eas_ca_armed")
    )}
    all_eas_cids = nws_cids | ca_cids

    if not all_eas_cids:
        return

    rc = _get_redis_client()

    # ── Fetch NWS alerts ──────────────────────────────────────────────────────
    # None = temporarily unavailable (another worker is fetching); [] = no alerts
    nws_alerts = []
    if nws_cids and zones:
        try:
            if rc:
                cached = rc.get(_EAS_REDIS_KEY)
                if cached:
                    nws_alerts = json.loads(cached)
                else:
                    if rc.set(_EAS_REDIS_LOCK, "1", nx=True, ex=30):
                        try:
                            nws_alerts = _fetch_nws_alerts(zones, severity_threshold)
                            rc.setex(_EAS_REDIS_KEY, _EAS_CACHE_TTL, json.dumps(nws_alerts))
                        except Exception as e:
                            logger.warning(f"[Ticker] EAS: NWS fetch failed: {e}")
                            nws_alerts = None  # unavailable
                    else:
                        nws_alerts = None  # another worker is fetching
            else:
                nws_alerts = _fetch_nws_alerts(zones, severity_threshold)
        except Exception as e:
            logger.warning(f"[Ticker] EAS: NWS fetch failed: {e}")
            nws_alerts = None

    # ── Fetch Weather Canada alerts ───────────────────────────────────────────
    ca_alerts = []
    if ca_cids and ca_ids:
        try:
            if rc:
                cached = rc.get(_EAS_CA_REDIS_KEY)
                if cached:
                    ca_alerts = json.loads(cached)
                else:
                    if rc.set(_EAS_CA_REDIS_LOCK, "1", nx=True, ex=30):
                        try:
                            ca_alerts = _fetch_weather_canada_alerts(ca_ids, severity_threshold, ca_language)
                            rc.setex(_EAS_CA_REDIS_KEY, _EAS_CACHE_TTL, json.dumps(ca_alerts))
                        except Exception as e:
                            logger.warning(f"[Ticker] EAS: Weather Canada fetch failed: {e}")
                            ca_alerts = None  # unavailable
                    else:
                        ca_alerts = None  # another worker is fetching
            else:
                ca_alerts = _fetch_weather_canada_alerts(ca_ids, severity_threshold, ca_language)
        except Exception as e:
            logger.warning(f"[Ticker] EAS: Weather Canada fetch failed: {e}")
            ca_alerts = None

    # ── Read persisted state and active viewers ───────────────────────────────
    current_state = {}
    if rc:
        try:
            raw = rc.get(_EAS_STATE_KEY)
            if raw:
                current_state = json.loads(raw)
        except Exception:
            pass

    streaming_ids = set()
    if rc:
        try:
            for k in rc.keys("channel_stream:*"):
                streaming_ids.add((k if isinstance(k, str) else k.decode()).split(":")[-1])
        except Exception:
            pass

    # ── Compute per-channel transitions ──────────────────────────────────────
    transitions = {}
    for cid in all_eas_cids:
        has_nws = cid in nws_cids
        has_ca  = cid in ca_cids

        # If the only source we depend on is temporarily unavailable, skip this cycle
        if has_nws and not has_ca and nws_alerts is None:
            continue
        if has_ca and not has_nws and ca_alerts is None:
            continue

        # Merge relevant alerts (treat None as [] for dual-armed channels)
        channel_alerts = []
        if has_nws:
            channel_alerts += (nws_alerts or [])
        if has_ca:
            channel_alerts += (ca_alerts or [])

        channel_worst = (
            sorted(channel_alerts, key=lambda a: _EAS_SEV.get(a["severity"], 0), reverse=True)[0]
            if channel_alerts else None
        )

        was_active = bool(current_state.get(cid))
        now_active = bool(channel_worst)

        if was_active == now_active:
            # No transition — refresh banner text on already-active channels
            if now_active:
                try:
                    _eas_write_alert(cid, _build_unique_alerts(channel_alerts), overlay_style)
                except Exception:
                    pass
            continue

        # Skip activation if nobody is watching; always allow clears
        if now_active and not was_active and cid not in streaming_ids:
            continue

        # Determine which tone to use when firing
        use_ca_tone = (
            has_ca and
            bool(ca_alerts) and
            any(a.get("source") == "EC" for a in channel_alerts)
        )
        transitions[cid] = (was_active, now_active, channel_worst, channel_alerts, use_ca_tone)

    if not transitions:
        return

    # Only one worker applies transitions
    if rc and not rc.set(_EAS_OWNER_KEY, "1", nx=True, ex=120):
        return

    from apps.channels.models import Channel
    from core.models import StreamProfile

    new_state = dict(current_state)
    changed = False

    for cid, (was_active, now_active, channel_worst, channel_alerts, use_ca_tone) in transitions.items():
        mapping = mappings.get(cid, {})
        try:
            channel = Channel.objects.filter(id=int(cid)).first()
            if not channel:
                continue

            unique_alerts = _build_unique_alerts(channel_alerts)
            label_color   = _EAS_BROADCAST_COLORS.get(
                channel_worst["severity"] if channel_worst else "Severe", "0xCC0000"
            )

            if now_active:
                if mapping.get("ticker_profile_id"):
                    _restore_profile(channel, mapping["original_profile_id"])
                    _delete_cloned_profile(mapping["ticker_profile_id"])
                    mapping.pop("ticker_profile_id", None)
                if mapping.get("eas_profile_id"):
                    _delete_cloned_profile(mapping["eas_profile_id"])
                    mapping.pop("eas_profile_id", None)

                orig = StreamProfile.objects.filter(id=mapping["original_profile_id"]).first()
                if not orig:
                    logger.warning(f"[Ticker] EAS: original profile missing for ch {cid}")
                    continue

                tone_wav = None
                if use_ca_tone and tone_interval > 0 and os.path.exists(_EAS_CA_TONE_FILE):
                    tone_wav = _EAS_CA_TONE_FILE

                eas_profile, _ = _clone_and_inject_eas(
                    channel.id, orig, channel.name,
                    tone_interval, overlay_style, label_color,
                    transcode_mode, tone_wav=tone_wav,
                )
                _assign_profile(channel, eas_profile)
                _eas_write_alert(cid, unique_alerts, overlay_style)
                mapping["eas_profile_id"] = eas_profile.id
                mappings[cid] = mapping
                new_state[cid] = True
                src = "EC" if use_ca_tone else "NWS"
                logger.info(f"[Ticker] EAS ALERT ({src}): {channel_worst['event']} — "
                            f"{channel_worst.get('area', '')[:60]} (ch {cid})")
                _eas_restart_channel(str(channel.uuid))

            else:
                eas_pid = mapping.get("eas_profile_id") or mapping.get("ticker_profile_id")
                _restore_profile(channel, mapping["original_profile_id"])
                if eas_pid:
                    _delete_cloned_profile(eas_pid)
                mapping.pop("eas_profile_id", None)
                mapping.pop("ticker_profile_id", None)
                mappings[cid] = mapping
                _eas_clear(cid)
                new_state[cid] = False
                logger.info(f"[Ticker] EAS: alert cleared — ch {cid}")
                _eas_restart_channel(str(channel.uuid))

            with _eas_lock:
                if now_active:
                    _eas_active[cid] = channel_worst["event"] if channel_worst else "EAS"
                else:
                    _eas_active.pop(cid, None)

            changed = True

        except Exception as e:
            logger.error(f"[Ticker] EAS: transition failed ch {cid}: {e}", exc_info=True)

    if changed:
        _save_mappings(mappings)
        if rc:
            try:
                rc.setex(_EAS_STATE_KEY, 3600, json.dumps(new_state))
            except Exception:
                pass

    if rc:
        try:
            rc.delete(_EAS_OWNER_KEY)
        except Exception:
            pass


def _build_unique_alerts(alerts):
    """Deduplicate alerts by event type, merging areas. Returns sorted worst-first list."""
    event_groups = {}
    for a in sorted(alerts, key=lambda x: _EAS_SEV.get(x["severity"], 0), reverse=True):
        ev = a["event"]
        if ev not in event_groups:
            event_groups[ev] = {"event": ev, "severity": a["severity"], "areas": []}
        area = (a.get("area") or "").strip()
        if area and area not in event_groups[ev]["areas"]:
            event_groups[ev]["areas"].append(area)
    return sorted(
        [{"event": d["event"], "severity": d["severity"], "area": "; ".join(d["areas"])}
         for d in event_groups.values()],
        key=lambda a: _EAS_SEV.get(a["severity"], 0), reverse=True,
    )


def _eas_sweep_loop(stop_event):
    # EAS Weather Alert — dedicated to jesmannstl
    # A Dispatcharr community member and severe weather enthusiast
    # whose passion for keeping people informed inspired this feature.
    # Rest easy.
    logger.info("[Ticker] EAS module initialized — for jesmannstl, who understood why this matters.")
    while not stop_event.is_set():
        interval = 60
        try:
            try:
                interval = max(15, int((_get_settings().get("eas_poll_interval") or 60)))
            except Exception:
                pass
            _eas_sweep()
        except Exception as e:
            logger.error(f"[Ticker] EAS loop error: {e}", exc_info=True)
        finally:
            try:
                from django.db import connection
                connection.close()
            except Exception:
                pass
        stop_event.wait(timeout=interval)


def _remove_sports_file(channel_id):
    for fname in (f"channel_{channel_id}_sports_labels.txt",
                  f"channel_{channel_id}_sports_abbrevs.txt",
                  f"channel_{channel_id}_sports_scores.txt",
                  f"channel_{channel_id}_sports_full.txt",
                  f"channel_{channel_id}_sports.txt"):   # legacy
        path = os.path.join(TICKER_DIR, fname)
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logger.warning(f"ticker: could not remove {path}: {e}")




# ---------------------------------------------------------------------------
# Channel cache helpers
# ---------------------------------------------------------------------------

CACHE_TTL = 24 * 3600  # was 7 days -- shortened alongside the live-fetch fix below,
                       # see that comment for why: a week-old cache still masks a
                       # mid-week SiriusXM lineup change for up to 6 more days.

# Bundled channel data ships inside the plugin directory alongside plugin.py, used
# only as a fallback when the live fetch below fails (e.g. no network reachability).
# Confirmed live 2026-09-04 (real incident on an EDM-managed fork of this plugin,
# ticker_agent): this bundled channels.json is a frozen snapshot from whenever the
# plugin was last built/released, with no live data source at all -- Refresh Channel
# Data only ever re-read this same frozen file. Running Sort Channels against it
# reverted already-corrected channel names back to their pre-reshuffle SXM numbers,
# because the bundled data still reflected the old lineup. Live-fetching from the
# same stellartunerlog.com source TICKER_CHANNEL_URL already uses for nowplaying
# data (below) is the actual fix -- reshaped into the same {name.lower(): {...}}
# structure the bundled file used, so _match_channel/_do_fill/_do_logos/
# _sort_channels all work unchanged regardless of which source supplied the data.
_BUNDLED_CHANNELS = os.path.join(_PLUGIN_DIR, "channels.json")
_BUNDLED_ALIASES  = os.path.join(_PLUGIN_DIR, "channel_aliases.json")


def _fetch_live_channel_catalog():
    """Fetch + reshape the live StellarTunerLog catalog (TICKER_CHANNEL_URL,
    defined below) into the same {name.lower(): {name, description, genre,
    sxm_number, seasonal, logo_url, sxm_logo_src, sxm_entity_id,
    lookaround_channel_id}} shape the bundled channels.json already uses."""
    req = urllib.request.Request(TICKER_CHANNEL_URL, headers={"User-Agent": "Ticker/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    channels = {}
    for entry in (data.get("channels") or {}).values():
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        channels[name.lower()] = {
            "name": name,
            "description": entry.get("description", ""),
            "genre": entry.get("primary_genre", ""),
            "seasonal": None,
            "sxm_number": entry.get("channel_number"),
            "logo_url": entry.get("logo_url", ""),
            "sxm_logo_src": "",
            "sxm_entity_id": entry.get("id", ""),
            "lookaround_channel_id": entry.get("guid", ""),
        }
    return channels


def _get_channel_data(force=False):
    if not force and os.path.exists(CHANNEL_CACHE_FILE):
        try:
            with open(CHANNEL_CACHE_FILE, encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("fetched_at", 0) + CACHE_TTL > time.time() and cache.get("channels"):
                return cache["channels"], cache.get("aliases", {})
        except Exception:
            pass

    channels, aliases = {}, {}

    # Live fetch is authoritative -- see _BUNDLED_CHANNELS's comment above for why.
    try:
        channels = _fetch_live_channel_catalog()
        logger.info(f"ticker: loaded {len(channels)} channels from live stellartunerlog.com fetch")
    except Exception as e:
        logger.warning(f"ticker: live channel catalog fetch failed ({e}) -- falling back to bundled channels.json")

    # Fallback: bundled snapshot, only used when the live fetch above failed entirely.
    if not channels and os.path.exists(_BUNDLED_CHANNELS):
        try:
            with open(_BUNDLED_CHANNELS, encoding="utf-8") as f:
                channels = json.load(f)
            logger.info(f"ticker: loaded {len(channels)} channels from bundled channels.json (fallback)")
        except Exception as e:
            logger.error(f"ticker: failed to load bundled channels.json: {e}")
    elif not channels:
        logger.warning("ticker: bundled channels.json not found and live fetch failed — channel matching unavailable")

    # Load aliases — bundled file, flat dict format
    if os.path.exists(_BUNDLED_ALIASES):
        try:
            with open(_BUNDLED_ALIASES, encoding="utf-8") as f:
                raw = json.load(f)
            # Support both flat dict and wrapped {"aliases": {...}} format
            aliases = raw.get("aliases", raw) if isinstance(raw, dict) and "aliases" in raw else raw
            logger.info(f"ticker: loaded {len(aliases)} aliases from bundled channel_aliases.json")
        except Exception as e:
            logger.error(f"ticker: failed to load bundled channel_aliases.json: {e}")

    if channels:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = CHANNEL_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "channels": channels, "aliases": aliases}, f)
        os.replace(tmp, CHANNEL_CACHE_FILE)

    return channels, aliases


def _match_channel(dispatcharr_name, channels, aliases):
    normalized = _normalize(dispatcharr_name)
    lookup_lower = dispatcharr_name.strip().lower()
    for alias, canonical in (aliases.items() if isinstance(aliases, dict) else []):
        if _normalize(alias) == normalized:
            normalized = _normalize(canonical)
            lookup_lower = canonical.strip().lower()
            break
    if isinstance(channels, dict):
        # channels.json keyed by name.lower() (e.g. "1st wave"), not fully normalized.
        # Dispatcharr channel names commonly carry stray leading/trailing whitespace
        # (M3U source formatting, or a previous un-prefixed Sort run) -- lookup_lower
        # above handles that via strip(). normalized strips ALL non-alnum chars
        # (including internal spaces), so it can only ever hit the dict's space-
        # preserving keys for single-word names; for multi-word names ("1st Wave",
        # "Yacht Rock Radio") fall back to a normalized scan so those match too.
        if lookup_lower in channels:
            return channels[lookup_lower]
        if normalized in channels:
            return channels[normalized]
        for name_key, entry in channels.items():
            if _normalize(name_key) == normalized:
                return entry
        return None
    for ch in channels:
        if _normalize(ch.get("name", "")) == normalized:
            return ch
    return None


def _normalize(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _get_mappings():
    try:
        if os.path.exists(MAPPINGS_FILE):
            with open(MAPPINGS_FILE, encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # Genuinely unparseable content (not a transient I/O error) — heal so we
        # don't re-log this on every sweep tick forever. Preserve the bad file
        # for forensics first.
        logger.error(f"ticker: mappings file is corrupt, resetting to empty: {e}")
        try:
            os.replace(MAPPINGS_FILE, MAPPINGS_FILE + ".corrupt")
        except OSError:
            pass
        _save_mappings({})
    except OSError as e:
        # Transient read failure (lock, permissions, race with a concurrent
        # writer) — do NOT touch the file; it may well be fine on the next read.
        logger.error(f"ticker: failed to read mappings: {e}")
    return {}


def _find_legacy_mappings():
    """Locate the old Tickarr plugin's own mappings.json, if that plugin is still installed
    alongside this one. Returns (path, mappings_dict) or (None, None) if not found/unreadable.
    """
    path = os.path.join(_PLUGINS_DIR, LEGACY_DATA_DIR_NAME, "mappings.json")
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            return path, json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        logger.error(f"ticker: found legacy Tickarr mappings but could not read them ({path}): {e}")
        return path, None


def _save_mappings(mappings):
    global _uuid_map_cache
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = MAPPINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(mappings, f, indent=2)
        os.replace(tmp, MAPPINGS_FILE)
        _uuid_map_cache = {"map": {}, "fetched_at": 0}  # force refresh on next scan
    except Exception as e:
        logger.error(f"ticker: failed to save mappings: {e}")


def _get_settings():
    from apps.plugins.models import PluginConfig
    config = PluginConfig.objects.filter(key=_PLUGIN_KEY).first()
    if not config or not config.settings:
        return {}
    settings = dict(config.settings)
    settings.pop("channel_mappings", None)
    settings.pop("channel_cache", None)
    return settings

# ---------------------------------------------------------------------------
# Sports ticker — ESPN client (Phase 3)
# ---------------------------------------------------------------------------

ESPN_PATHS = {
    # Football
    'nfl':        'sports/football/nfl',
    'ncaafb':     'sports/football/college-football',
    'cfl':        'sports/football/cfl',
    'ufl':        'sports/football/ufl',
    'xfl':        'sports/football/xfl',
    # Basketball
    'nba':        'sports/basketball/nba',
    'wnba':       'sports/basketball/wnba',
    'ncaamb':     'sports/basketball/mens-college-basketball',
    'ncaawb':     'sports/basketball/womens-college-basketball',
    'nbagl':      'sports/basketball/nba-development',
    # Baseball / Softball
    'mlb':        'sports/baseball/mlb',
    'ncaabase':   'sports/baseball/college-baseball',
    'ncaasb':     'sports/softball/college-softball',
    # Hockey
    'nhl':        'sports/hockey/nhl',
    'ncaahm':     'sports/hockey/mens-college-hockey',
    'ncaahw':     'sports/hockey/womens-college-hockey',
    # Soccer — North America
    'mls':        'sports/soccer/usa.1',
    'nwsl':       'sports/soccer/usa.nwsl',
    'ligamx':     'sports/soccer/mex.1',
    # Soccer — Europe Big 5
    'epl':        'sports/soccer/eng.1',
    'laliga':     'sports/soccer/esp.1',
    'bundesliga': 'sports/soccer/ger.1',
    'seriea':     'sports/soccer/ita.1',
    'ligue1':     'sports/soccer/fra.1',
    # Soccer — Europe Other
    'eredivisie': 'sports/soccer/ned.1',
    'primlg':     'sports/soccer/por.1',
    'sco1':       'sports/soccer/sco.1',
    # Soccer — UEFA
    'ucl':        'sports/soccer/uefa.champions',
    'uel':        'sports/soccer/uefa.europa',
    'uecl':       'sports/soccer/uefa.europa.conf',
    # Soccer — International
    'fifawc':     'sports/soccer/fifa.world',
    'fifawwc':    'sports/soccer/fifa.wwc',
    'copa':       'sports/soccer/conmebol.america',
    'concacaf':   'sports/soccer/concacaf.champions',
    'lgscup':     'sports/soccer/concacaf.leagues.cup',
    # Golf
    'pga':        'sports/golf/pga',
    'lpga':       'sports/golf/lpga',
    'dpwt':       'sports/golf/euro',
    'liv':        'sports/golf/liv',
    'pgachamp':   'sports/golf/champions-tour',
    # Motorsports
    'f1':         'sports/racing/f1',
    'indycar':    'sports/racing/irl',
    # Combat
    'ufc':        'sports/mma/ufc',
    # Tennis
    'atp':        'sports/tennis/atp',
    'wta':        'sports/tennis/wta',
    # College other
    'ncaavb':     'sports/volleyball/womens-college-volleyball',
    'ncaalaxm':   'sports/lacrosse/mens-college-lacrosse',
    'ncaalaxw':   'sports/lacrosse/womens-college-lacrosse',
    'pll':        'sports/lacrosse/pll',
}
LABELS = {
    # Football
    'nfl': 'NFL', 'ncaafb': 'NCAAF', 'cfl': 'CFL', 'ufl': 'UFL', 'xfl': 'XFL',
    # Basketball
    'nba': 'NBA', 'wnba': 'WNBA', 'ncaamb': 'NCAAB', 'ncaawb': 'NCAA WBB', 'nbagl': 'G League',
    # Baseball / Softball
    'mlb': 'MLB', 'ncaabase': 'NCAA Baseball', 'ncaasb': 'NCAA Softball',
    # Hockey
    'nhl': 'NHL', 'ncaahm': 'NCAA Hockey', 'ncaahw': 'NCAA WHky',
    # Soccer
    'mls': 'MLS', 'nwsl': 'NWSL', 'ligamx': 'Liga MX',
    'epl': 'EPL', 'laliga': 'La Liga', 'bundesliga': 'Bundesliga',
    'seriea': 'Serie A', 'ligue1': 'Ligue 1',
    'eredivisie': 'Eredivisie', 'primlg': 'Primeira Liga', 'sco1': 'Scottish Prem',
    'ucl': 'UCL', 'uel': 'UEL', 'uecl': 'UECL',
    'fifawc': 'FIFA WC', 'fifawwc': 'FIFA WWC',
    'copa': 'Copa America', 'concacaf': 'CONCACAF CC', 'lgscup': 'Leagues Cup',
    # Golf
    'pga': 'PGA Tour', 'lpga': 'LPGA', 'dpwt': 'DP World Tour',
    'liv': 'LIV Golf', 'pgachamp': 'PGA Champ',
    # Motorsports
    'f1': 'Formula 1', 'indycar': 'IndyCar',
    # Combat
    'ufc': 'UFC',
    # Tennis
    'atp': 'ATP', 'wta': 'WTA',
    # College other
    'ncaavb': 'NCAA VB', 'ncaalaxm': 'NCAA Lax M', 'ncaalaxw': 'NCAA Lax W', 'pll': 'PLL',
    # NASCAR (separate handler)
    'nascar': 'NASCAR',
}
KNOWN_SPORTS = list(ESPN_PATHS.keys()) + ['nascar']

_sports_text_cache = {"key": None, "labels": "", "abbrevs": "", "scores": "", "full": "", "fetched_at": 0.0}
SPORTS_CACHE_TTL = 30  # seconds

# EAS globals
NWS_ALERTS_URL  = "https://api.weather.gov/alerts/active"
NWS_UA          = "Ticker/0.2 (github.com/jstevenscl/ticker)"
_EAS_SEV        = {"Unknown": 0, "Minor": 1, "Moderate": 2, "Severe": 3, "Extreme": 4}
_eas_active     = {}   # channel_id → alert event string when alert is active
_eas_lock       = threading.Lock()
_TICKER_FIXED_LEN = 600  # all ticker strings are always exactly this many chars
# Fixed length keeps text_w constant across reloads — prevents scroll position jumping
# when game count changes between ESPN polls. Content beyond 600 chars is truncated
# (live games come first so the most important scores are always visible).


_SPORTS_TRANSCODE_PREFIXES = {
    "full":    "",
    "1080p30": "fps=fps=30,",
    "720p":    "fps=fps=30,scale=-2:720:flags=fast_bilinear,",
    "720p30":  "fps=fps=30,scale=-2:720:flags=fast_bilinear,",
}


def _build_sports_filter(channel_id, position="bottom", fontsize=36,
                         labelcolor="#ffd700", abbrcolor="#00d4ff",
                         color_mode="single", transcode_mode="1080p30",
                         ticker_style="scrolling"):
    y_map = {"top": "30", "center": "(h-text_h)/2", "bottom": "h-text_h-30"}
    y_expr = y_map.get(position, "h-text_h-30")
    fs = int(fontsize)
    x_expr = "(w-text_w)/2" if ticker_style == "static" else "w-mod(t*100\\,w+text_w)"
    prefix = _SPORTS_TRANSCODE_PREFIXES.get(transcode_mode, "fps=fps=30,")
    use_multi = (color_mode == "multi") and bool(_FONT_MONO_BOLD)
    if use_multi:
        tmpl_scores  = _SPORTS_SCORES_LAYER.replace("w-mod(t*100\\\\,w+text_w)", x_expr).replace("w-mod(t*100\\,w+text_w)", x_expr)
        tmpl_abbrevs = _SPORTS_ABBREVS_LAYER.replace("w-mod(t*100\\\\,w+text_w)", x_expr).replace("w-mod(t*100\\,w+text_w)", x_expr)
        tmpl_labels  = _SPORTS_LABELS_LAYER.replace("w-mod(t*100\\\\,w+text_w)", x_expr).replace("w-mod(t*100\\,w+text_w)", x_expr)
        kwargs = dict(font=_FONT_MONO_BOLD, ticker_dir=TICKER_DIR,
                      fontsize=fs, channel_id=channel_id, y_expr=y_expr)
        scores_layer  = tmpl_scores.format(**kwargs)
        abbrevs_layer = tmpl_abbrevs.format(**kwargs, abbrcolor=abbrcolor)
        labels_layer  = tmpl_labels.format(**kwargs, labelcolor=labelcolor)
        return f"{prefix}{scores_layer},{abbrevs_layer},{labels_layer}"
    else:
        font = _FONT_MONO_BOLD or _FONT_BOLD
        tmpl = _SPORTS_SINGLE_LAYER.replace("w-mod(t*100\\\\,w+text_w)", x_expr).replace("w-mod(t*100\\,w+text_w)", x_expr)
        return prefix + tmpl.format(
            font=font, ticker_dir=TICKER_DIR,
            fontsize=fs, channel_id=channel_id, y_expr=y_expr,
        )


def _game_seg_triple(away_abbr, away_score, home_abbr, home_score, suffix):
    """Build equal-length (label_seg, abbrev_seg, score_seg) for one game.
    label_seg  = all spaces (labels live at the sport section level, not game level).
    abbrev_seg = team abbreviations at their positions, spaces elsewhere.
    score_seg  = scores/status at their positions, spaces for abbreviations.
    All three strings have identical character count — monospace scroll sync.
    """
    mid  = f" {away_score} @ "
    rest = f" {home_score} {suffix}"
    total = len(away_abbr) + len(mid) + len(home_abbr) + len(rest)
    label_seg  = " " * total
    abbrev_seg = f"{away_abbr}{' ' * len(mid)}{home_abbr}{' ' * len(rest)}"
    score_seg  = f"{' ' * len(away_abbr)}{mid}{' ' * len(home_abbr)}{rest}"
    return label_seg, abbrev_seg, score_seg


def _sport_section_triple(sport_label, game_triples):
    """Assemble per-game triples into a full sport section triple.
    Prepends 'SPORT: ' — the label goes in the label layer, spaces fill the others.
    """
    lbl = sport_label + ":"
    sep = "  "
    l_parts, a_parts, s_parts = [], [], []
    for (l, a, s) in game_triples:
        l_parts.append(l)
        a_parts.append(a)
        s_parts.append(s)
    combined_l = sep.join(l_parts)
    combined_a = sep.join(a_parts)
    combined_s = sep.join(s_parts)
    prefix_len = len(lbl) + 1  # "NFL: "
    label_seg  = lbl + " " * (1 + len(combined_s))
    abbrev_seg = " " * prefix_len + combined_a
    score_seg  = " " * prefix_len + combined_s
    return label_seg, abbrev_seg, score_seg


def _fetch_sports_text(sports_list, favorites=""):
    """Fetch scores from ESPN (and NASCAR live feed).

    Returns (labels_text, abbrevs_text, scores_text, full_text) — equal-length strings.
    All three text strings are padded to the same length for monospace scroll sync.
    """
    global _sports_text_cache
    cache_key = (tuple(sorted(sports_list)), (favorites or "").strip().upper())
    now = time.time()
    if (_sports_text_cache["key"] == cache_key and
            now - _sports_text_cache["fetched_at"] < SPORTS_CACHE_TTL):
        return (_sports_text_cache["labels"],
                _sports_text_cache["abbrevs"],
                _sports_text_cache["scores"],
                _sports_text_cache.get("full", ""))

    fav_set = set(a.strip().upper() for a in favorites.split(",") if a.strip()) if favorites else set()
    live_triples  = []
    final_triples = []

    for sport_id in sports_list:
        label = LABELS.get(sport_id, sport_id.upper())
        try:
            if sport_id in ESPN_PATHS:
                url = f'https://site.api.espn.com/apis/site/v2/{ESPN_PATHS[sport_id]}/scoreboard'
                req = urllib.request.Request(url, headers={"User-Agent": "Ticker/0.1"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.loads(r.read())
                events = data.get('events', [])
                live_games  = []
                final_games = []
                for ev in events[:30]:
                    comp = ev.get('competitions', [{}])[0]
                    competitors = comp.get('competitors', [])
                    if len(competitors) < 2:
                        continue
                    home = next((c for c in competitors if c.get('homeAway') == 'home'), competitors[0])
                    away = next((c for c in competitors if c.get('homeAway') == 'away'), competitors[1])
                    away_abbr = away.get('team', {}).get('abbreviation', '?')
                    home_abbr = home.get('team', {}).get('abbreviation', '?')
                    if fav_set and away_abbr.upper() not in fav_set and home_abbr.upper() not in fav_set:
                        continue
                    away_score = away.get('score', '')
                    home_score = home.get('score', '')
                    st    = comp.get('status', {}).get('type', {})
                    state = st.get('state', '')
                    detail = st.get('shortDetail', '')
                    if state == 'in':
                        live_games.append(
                            _game_seg_triple(away_abbr, away_score, home_abbr, home_score, f"({detail})"))
                    elif state == 'post':
                        final_games.append(
                            _game_seg_triple(away_abbr, away_score, home_abbr, home_score, "FINAL"))
                if live_games:
                    live_triples.append(_sport_section_triple(label, live_games))
                if final_games:
                    final_triples.append(_sport_section_triple(label, final_games))

            elif sport_id == 'nascar':
                nascar_headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'application/json',
                    'Referer': 'https://www.nascar.com/',
                    'Origin': 'https://www.nascar.com',
                }
                req = urllib.request.Request(
                    'https://cf.nascar.com/live/feeds/live-feed.json',
                    headers=nascar_headers,
                )
                with urllib.request.urlopen(req, timeout=8) as r:
                    lf = json.loads(r.read())
                if lf.get('series_id') == 1 and 1 <= lf.get('flag_state', 0) <= 8:
                    run_name = lf.get('run_name', '')
                    lap   = lf.get('lap_number', 0)
                    total = lf.get('laps_in_race', 0)
                    vehicles = sorted(lf.get('vehicles', []), key=lambda v: v.get('running_position', 99))
                    d_strs = []
                    for v in vehicles[:5]:
                        pos    = v.get('running_position', '')
                        driver = v.get('driver', {})
                        name   = (driver.get('full_name') or
                                  f"{driver.get('first_name', '')} {driver.get('last_name', '')}").strip()
                        d_strs.append(f'P{pos} {name.split()[-1] if name else "?"}')
                    lap_str = f'Lap {lap}/{total}' if total else ''
                    if d_strs:
                        lbl     = f"NASCAR ({run_name}):"
                        content = f" {'  '.join(d_strs)}  {lap_str}"
                        seg_len = len(lbl) + len(content)
                        live_triples.append((
                            lbl + " " * len(content),  # label layer
                            " " * seg_len,             # abbrev layer (no discrete abbrevs)
                            " " * len(lbl) + content,  # score layer
                        ))

        except Exception as e:
            logger.warning(f"ticker: sports fetch error for {sport_id}: {e}")

    all_triples = live_triples + final_triples
    if not all_triples:
        labels_text = abbrevs_text = scores_text = ""
    else:
        sep_scores = "    |    "   # 9 chars ASCII — visible separator in scores layer
        sep_blank  = "         "   # 9 spaces — invisible in label/abbrev layers

        unit_l = sep_blank.join(t[0] for t in all_triples)
        unit_a = sep_blank.join(t[1] for t in all_triples)
        unit_s = sep_scores.join(t[2] for t in all_triples)

        rep_l, rep_a, rep_s = unit_l, unit_a, unit_s
        while len(rep_s) < _TICKER_FIXED_LEN:
            rep_l += sep_blank  + unit_l
            rep_a += sep_blank  + unit_a
            rep_s += sep_scores + unit_s

        # Enforce exact fixed length across all three layers.
        # Constant char count → constant text_w → scroll position never jumps on reload.
        labels_text  = rep_l[:_TICKER_FIXED_LEN].ljust(_TICKER_FIXED_LEN)
        abbrevs_text = rep_a[:_TICKER_FIXED_LEN].ljust(_TICKER_FIXED_LEN)
        scores_text  = rep_s[:_TICKER_FIXED_LEN].ljust(_TICKER_FIXED_LEN)

    # Build full merged text for single-layer fallback (used when no mono font).
    # Each position has at most one non-space char across the three layers.
    if labels_text:
        merged_chars = []
        for l, a, s in zip(labels_text, abbrevs_text, scores_text):
            merged_chars.append(l if l != ' ' else (a if a != ' ' else s))
        full_text = "".join(merged_chars)
    else:
        full_text = ""

    _sports_text_cache = {
        "key":        cache_key,
        "labels":     labels_text,
        "abbrevs":    abbrevs_text,
        "scores":     scores_text,
        "full":       full_text,
        "has_live":   bool(live_triples),
        "target_len": len(labels_text),
        "fetched_at": now,
    }
    return labels_text, abbrevs_text, scores_text, full_text


def _has_live_games(sports_list, favorites=""):
    """Return True if any live game is in progress for the given sports/favorites combo."""
    cache_key = (tuple(sorted(sports_list)), (favorites or "").strip().upper())
    now = time.time()
    if (_sports_text_cache["key"] == cache_key and
            now - _sports_text_cache["fetched_at"] < SPORTS_CACHE_TTL):
        return _sports_text_cache.get("has_live", False)
    try:
        _fetch_sports_text(sports_list, favorites)
    except Exception:
        return False
    return _sports_text_cache.get("has_live", False)

# ---------------------------------------------------------------------------
# ticker.com data client (replaces xmplaylist.com)
# ---------------------------------------------------------------------------

TICKER_NOWPLAYING_URL = "https://stellartunerlog.com/nowplaying.json"
TICKER_CHANNEL_URL    = "https://stellartunerlog.com/channels.json"
TICKER_SXM_EPG_URL    = "https://jstevenscl.github.io/ticker/lib/satellite_radio_epg.xml"
TICKER_SXM_SOURCE     = "Ticker: Satellite Radio"
STATION_CACHE_TTL      = 24 * 3600
STATION_CACHE_FILE     = os.path.join(_DATA_DIR, "station_cache.json")
NOWPLAYING_CACHE_TTL   = 15   # seconds — 15s cuts worst-case song display lag vs 30s source update cycle

XMPLAYLIST_STATION_URL  = "https://xmplaylist.com/api/station/{deeplink}"
XMPLAYLIST_MIN_INTERVAL = 1.5  # seconds between per-channel requests

# cut_type values that indicate non-song content (talk, ads, promos, etc.)
_NON_SONG_CUT_TYPES = frozenset({"talk", "exp", "perm", "pgm_segment", "link", "spot", "promo"})
# subset where STL artist field contains an actual program/show name worth displaying
# "spot"/"promo" excluded — their artist field contains ad/promo copy, not program names
_PROGRAM_CUT_TYPES  = frozenset({"talk", "pgm_segment", "exp", "perm", "link"})

_station_cache    = {"fetched_at": 0, "stations": []}
_nowplaying_cache = {"fetched_at": 0.0, "stations": {}}
_nowplaying_lock  = threading.Lock()
_xmplaylist_lock  = threading.Lock()
_xmplaylist_last  = {"time": 0.0}


def _get_stations(force=False):
    """Fetch channel catalog from ticker.com/channels.json (24h TTL, disk-cached)."""
    global _station_cache
    now = time.time()
    if not force and _station_cache["fetched_at"] + STATION_CACHE_TTL > now and _station_cache["stations"]:
        return _station_cache["stations"]
    if not force and os.path.exists(STATION_CACHE_FILE):
        try:
            with open(STATION_CACHE_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("fetched_at", 0) + STATION_CACHE_TTL > now and cached.get("stations"):
                _station_cache = cached
                logger.debug(f"ticker: channel list loaded from disk ({len(cached['stations'])} channels)")
                return cached["stations"]
        except Exception:
            pass
    try:
        req = urllib.request.Request(TICKER_CHANNEL_URL, headers={"User-Agent": "Ticker/0.1"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        stations = list((data.get("channels") or {}).values())
        _station_cache = {"fetched_at": now, "stations": stations}
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = STATION_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_station_cache, f)
        os.replace(tmp, STATION_CACHE_FILE)
        logger.info(f"ticker: fetched {len(stations)} channels from ticker.com")
        return stations
    except Exception as e:
        logger.error(f"ticker: channel list fetch failed: {e}")
        return _station_cache.get("stations") or []


def _get_nowplaying_bulk():
    """Fetch all channels' now-playing from ticker.com in one request (30s TTL)."""
    global _nowplaying_cache
    now = time.time()
    with _nowplaying_lock:
        if now - _nowplaying_cache["fetched_at"] < NOWPLAYING_CACHE_TTL and _nowplaying_cache["stations"]:
            return _nowplaying_cache["stations"]
    try:
        req = urllib.request.Request(TICKER_NOWPLAYING_URL, headers={"User-Agent": "Ticker/0.1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        stations = data.get("stations") or {}
        with _nowplaying_lock:
            _nowplaying_cache = {"fetched_at": time.time(), "stations": stations}
        return stations
    except Exception as e:
        logger.warning(f"ticker: nowplaying bulk fetch failed: {e}")
        return _nowplaying_cache.get("stations") or {}


def _xmplaylist_fetch(deeplink):
    """Per-channel xmplaylist.com fallback with rate limiting.
    Returns (artist, song) strings, or (None, None) on failure.
    Only called when stellartunerlog.com bulk data is available but missing this channel.
    """
    global _xmplaylist_last
    with _xmplaylist_lock:
        elapsed = time.time() - _xmplaylist_last["time"]
        if elapsed < XMPLAYLIST_MIN_INTERVAL:
            time.sleep(XMPLAYLIST_MIN_INTERVAL - elapsed)
        try:
            url = XMPLAYLIST_STATION_URL.format(deeplink=deeplink)
            req = urllib.request.Request(url, headers={"User-Agent": "Ticker/0.1"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            _xmplaylist_last["time"] = time.time()
            if data and isinstance(data, list) and data[0].get("track"):
                track  = data[0]["track"]
                artist = (track.get("artists") or [""])[0]
                song   = track.get("title", "")
                return artist or "", song or ""
        except Exception as e:
            logger.debug(f"ticker: xmplaylist fallback failed for {deeplink}: {e}")
        _xmplaylist_last["time"] = time.time()
    return None, None


def _match_station_by_uuid(uuid, stations):
    # ticker.com channels.json uses "guid" for the SiriusXM UUID
    for s in stations:
        if s.get("guid") == uuid:
            return s
    return None


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _match_station_by_name(channel_name, stations):
    if not stations:
        return None
    n = _norm(channel_name)

    # Pass 1: exact normalized name, deeplink_id, or id match
    for s in stations:
        if (_norm(s.get("name", "")) == n
                or _norm(s.get("deeplink_id", "")) == n
                or _norm(s.get("id", "")) == n):
            return s

    # Pass 2: strip leading "siriusxm" from channel name
    n2 = re.sub(r"^siriusxm", "", n)
    if n2 and n2 != n:
        for s in stations:
            sn = _norm(s.get("name", ""))
            dl = _norm(s.get("deeplink_id", ""))
            di = _norm(s.get("id", ""))
            if sn == n2 or dl == n2 or di == n2:
                return s

    # Pass 3: strip leading "siriusxm" from station name
    for s in stations:
        sn = re.sub(r"^siriusxm", "", _norm(s.get("name", "")))
        if sn and sn == n:
            return s

    # Pass 4: channel number match
    try:
        num_match = re.search(r'\b(\d+)\b', channel_name)
        if num_match:
            num = int(num_match.group(1))
            for s in stations:
                if s.get("channel_number") == num:
                    return s
    except Exception:
        pass

    # Pass 5: one name fully contains the other (min 5 chars to avoid false positives)
    if len(n) >= 5:
        for s in stations:
            sn = _norm(s.get("name", ""))
            if len(sn) >= 5 and (sn in n or n in sn):
                return s

    # Pass 6: strip trailing "radio" from channel name, then rematch
    n6 = re.sub(r"radio$", "", n)
    if n6 and n6 != n:
        for s in stations:
            if (_norm(s.get("name", "")) == n6
                    or _norm(s.get("deeplink_id", "")) == n6
                    or _norm(s.get("id", "")) == n6):
                return s

    # Pass 7: strip trailing "radio" from station name/id, then rematch
    for s in stations:
        sn = re.sub(r"radio$", "", _norm(s.get("name", "")))
        dl = re.sub(r"radio$", "", _norm(s.get("deeplink_id", "")))
        di = re.sub(r"radio$", "", _norm(s.get("id", "")))
        if (sn and sn == n) or (dl and dl == n) or (di and di == n):
            return s

    return None


# ---------------------------------------------------------------------------
# Scheduler / poll loop
# ---------------------------------------------------------------------------

_scheduler_thread = None
_stop_event = threading.Event()
_redis_client_cache = None
_redis_client_lock = threading.Lock()

STALE_THRESHOLD   = 120   # seconds — channels not updated in this long get auto-refreshed
STALE_BATCH_SIZE  = 10    # max stale channels to recover per sweep

# UUID→integer channel ID cache (refreshed every 5 min)
_uuid_map_cache = {"map": {}, "fetched_at": 0}
UUID_MAP_TTL = 300

# Distributed lock keys — one winner per loop across all uWSGI workers
SWEEP_LOCK_KEY   = "ticker:sweep_lock"
FAST_LOCK_KEY    = "ticker:fast_lock"
SPORTS_LOCK_KEY  = "ticker:sports_lock"
_SWEEP_LOCK_TTL  = 45   # SWEEP_SLEEP(15) + up to 10s poll + 20s buffer; refreshed post-sweep
_FAST_LOCK_TTL   = 10   # renewed every 2s tick; 10s crash-recovery window
_IDLE_RESTORE_DELAY = 30  # seconds with no active viewers before restoring passthrough profile
_SPORTS_LOCK_TTL = 60   # SPORTS_SWEEP_SLEEP(30) + poll time + buffer; refreshed post-sweep


def _get_redis_client():
    global _redis_client_cache
    with _redis_client_lock:
        if _redis_client_cache is not None:
            try:
                _redis_client_cache.ping()
                return _redis_client_cache
            except Exception:
                _redis_client_cache = None
        try:
            from django_redis import get_redis_connection
            rc = get_redis_connection("default")
            rc.ping()
            _redis_client_cache = rc
            return rc
        except Exception:
            pass
        try:
            from django.conf import settings as _settings
            import redis as _redis
            url = (getattr(_settings, "REDIS_URL", None)
                   or getattr(_settings, "CACHES", {}).get("default", {}).get("LOCATION")
                   or "redis://redis:6379/0")
            rc = _redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
            rc.ping()
            _redis_client_cache = rc
            return rc
        except Exception:
            pass
        return None


def _redis_lock_acquire_or_refresh(rc, key, ttl):
    """Acquire or renew a Redis distributed lock for this process.
    Returns True if this worker holds the lock, False if another worker holds it.
    On first call uses NX-set; on subsequent calls by the same pid, refreshes TTL."""
    my_pid = str(os.getpid())
    if rc.set(key, my_pid, nx=True, ex=ttl):
        return True
    current = rc.get(key)
    if current and current.decode() == my_pid:
        rc.expire(key, ttl)
        return True
    return False


def _get_uuid_to_id_map(mappings):
    """Returns {uuid_str: int_channel_id} for all currently mapped channels.
    Cached for UUID_MAP_TTL seconds to avoid hitting the DB on every 2s tick."""
    global _uuid_map_cache
    now = time.time()
    if now - _uuid_map_cache["fetched_at"] < UUID_MAP_TTL and _uuid_map_cache["map"]:
        return _uuid_map_cache["map"]
    try:
        from apps.channels.models import Channel
        mapped_ids = set()
        for cid in mappings.keys():
            try:
                mapped_ids.add(int(cid))
            except (ValueError, TypeError):
                pass
        result = {}
        for ch in Channel.objects.filter(id__in=mapped_ids):
            uuid_val = getattr(ch, "uuid", None)
            if uuid_val:
                result[str(uuid_val).lower()] = ch.id
        _uuid_map_cache = {"map": result, "fetched_at": now}
        logger.debug(f"ticker: uuid map refreshed — {len(result)} entries")
        return result
    except Exception as e:
        logger.debug(f"ticker: uuid map error: {e}")
        return _uuid_map_cache.get("map", {})
    finally:
        # Close the thread-local DB connection so it doesn't sit open indefinitely.
        # Background threads are never part of Django's request/response cycle, so
        # connections are never automatically cleaned up without this.
        try:
            from django.db import connection
            connection.close()
        except Exception:
            pass


def _redis_scan_active():
    """Scan Redis for ts_proxy:channel:{UUID}:activity keys. Returns set of
    integer channel IDs with active streams, or None if Redis is unavailable."""
    rc = _get_redis_client()
    if rc is None:
        return None
    mappings = _get_mappings()
    if not mappings:
        return set()
    uuid_to_id = _get_uuid_to_id_map(mappings)
    if not uuid_to_id:
        return set()
    active = set()
    try:
        # v0.25+ uses live:channel:*:activity; v0.24 used ts_proxy:channel:*:activity
        for pattern in ("live:channel:*:activity", "ts_proxy:channel:*:activity"):
            for raw_key in rc.scan_iter(pattern, count=200):
                key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                parts = key.split(":")
                if len(parts) < 4:
                    continue
                uuid = parts[2].lower()
                cid = uuid_to_id.get(uuid)
                if cid is not None:
                    active.add(cid)
    except Exception as e:
        logger.debug(f"ticker: Redis scan error: {e}")
        return None
    return active


def _build_channel_list(channel_mappings):
    ch = []
    for cid_str, mapping in channel_mappings.items():
        if not mapping or mapping.get("type", "nowplaying") != "nowplaying":
            continue
        deeplink = mapping.get("xm_deeplink")
        if not deeplink:
            continue
        try:
            channel_id = int(cid_str)
        except (ValueError, TypeError):
            continue
        ch.append((channel_id, deeplink, mapping.get("channel_name", ""),
                   mapping.get("channel_description", "")))
    return ch


def _write_on_air(channel_id, channel_name, title="", subtitle=""):
    """Write 'On Air' overlay for non-song content.
    title   → artist slot (show/match name)
    subtitle → song slot  (live score line, segment title, etc.)
    SiriusXM sports channels send match + score as artist + title fields."""
    _ensure_dirs()
    _atomic_write(f"channel_{channel_id}_header.txt",  "On Air")
    _atomic_write(f"channel_{channel_id}_artist.txt",  _truncate(title or "", 38))
    sub = (subtitle or "").strip()
    _atomic_write(f"channel_{channel_id}_song.txt",    _truncate(sub, 45) if sub else "")
    _atomic_write(f"channel_{channel_id}_channel.txt", channel_name or "")


def _fetch_and_write(args):
    channel_id, deeplink, channel_name, channel_description = args
    # EAS alert is active — preserve alert content, skip now-playing update
    with _eas_lock:
        if _eas_active.get(str(channel_id)):
            return
    try:
        rc = _get_redis_client()
        if rc is not None:
            if not rc.set(f"ticker:last_fetch:{channel_id}", "1", nx=True, ex=8):
                return  # another worker fetched this channel in the last 8s — skip
        stations = _get_nowplaying_bulk()
        station  = stations.get(deeplink) if deeplink else None
        if station:
            cut_type = station.get("cut_type", "")
            if (cut_type or "").lower() in _NON_SONG_CUT_TYPES:
                if (cut_type or "").lower() in _PROGRAM_CUT_TYPES:
                    # talk/pgm_segment: artist = show/match name, title = score or segment
                    # SiriusXM sports channels send e.g. artist="Norway v Senegal",
                    # title="NOR 3 - SEN 1 • 2H" — both fields are meaningful
                    program  = station.get("artist", "") or ""
                    score    = station.get("title",  "") or ""
                    _write_on_air(channel_id, channel_name,
                                  title=program.strip(), subtitle=score.strip())
                else:
                    # spot/promo/exp/etc: artist = ad or promo copy, not useful
                    _write_on_air(channel_id, channel_name, title="")
            else:
                artist = station.get("artist", "") or ""
                song   = station.get("title",  "") or ""
                _write_nowplaying(channel_id, artist, song, channel_name)
        elif stations and deeplink:
            # Bulk is up but this deeplink is absent — try xmplaylist per-channel
            artist, song = _xmplaylist_fetch(deeplink)
            if artist is not None or song is not None:
                _write_nowplaying(channel_id, artist or "", song or "", channel_name)
            else:
                logger.warning(f"ticker: no data for {channel_name} ({deeplink}) from either source")
                path = os.path.join(TICKER_DIR, f"channel_{channel_id}_song.txt")
                if os.path.exists(path):
                    os.utime(path, None)
        else:
            logger.warning(f"ticker: no data for {channel_name} ({deeplink})")
            path = os.path.join(TICKER_DIR, f"channel_{channel_id}_song.txt")
            if os.path.exists(path):
                os.utime(path, None)
    except Exception as e:
        logger.warning(f"ticker: fetch failed for {channel_name} ({deeplink}): {e}")


def _poll_channels(ch_list):
    for args in ch_list:
        _fetch_and_write(args)


def _fast_loop(stop_event):
    """Every 1s: scan Redis for newly active streams and poll them immediately.
    For on-demand nowplaying channels, clones the overlay profile on first viewer connect.
    On first tick, just records current state without polling (avoids startup burst)."""
    known_active = None  # None = uninitialized; skip polling on first observation
    while not stop_event.wait(timeout=1):
        try:
            rc = _get_redis_client()
            if rc is not None and not _redis_lock_acquire_or_refresh(rc, FAST_LOCK_KEY, _FAST_LOCK_TTL):
                continue
            mappings = _get_mappings()
            if not mappings:
                continue
            ch_list = _build_channel_list(mappings)
            ch_ids = {ch[0]: ch for ch in ch_list}

            current_active = _redis_scan_active()
            if current_active is None:
                known_active = None
                continue  # Redis unavailable — sweep loop handles everything
            sports_cids = {int(k) for k, v in mappings.items()
                           if v and v.get("type") == "sports"}
            current_active &= (set(ch_ids.keys()) | sports_cids)
            if known_active is None:
                known_active = current_active  # baseline — don't poll on first tick
                continue
            newly_active = current_active - known_active
            known_active = current_active
            if newly_active:
                # On-demand nowplaying: clone overlay profile for newly active channels
                mappings_changed = False
                for cid in list(newly_active):
                    mapping = mappings.get(str(cid))
                    if not mapping:
                        continue
                    if mapping.get("type") != "nowplaying":
                        continue
                    if mapping.get("ticker_profile_id"):
                        continue  # already has an overlay profile
                    if mapping.get("eas_profile_id"):
                        continue  # EAS alert is active — don't touch the profile
                    try:
                        from apps.channels.models import Channel as _Ch
                        from core.models import StreamProfile as _SP
                        channel = _Ch.objects.filter(id=cid).first()
                        orig = _SP.objects.filter(id=mapping["original_profile_id"]).first()
                        if channel and orig:
                            cloned, _ = _clone_and_inject(cid, orig, channel.name)
                            _assign_profile(channel, cloned)
                            _write_fallback(cid, channel.name, mapping.get("channel_description", ""))
                            mapping["ticker_profile_id"] = cloned.id
                            mapping.pop("no_viewer_since", None)
                            mappings[str(cid)] = mapping
                            mappings_changed = True
                            logger.info(f"ticker: on-demand overlay activated ch{cid} ({channel.name})")
                            _restart_channel_stream_async(channel, "NP")
                    except Exception as e:
                        logger.warning(f"ticker: on-demand clone failed ch{cid}: {e}")

                # On-demand sports: clone overlay profile for newly active channels
                for cid in list(newly_active):
                    mapping = mappings.get(str(cid))
                    if not mapping:
                        continue
                    if mapping.get("type") != "sports":
                        continue
                    if mapping.get("ticker_profile_id"):
                        continue  # already has overlay
                    if mapping.get("eas_profile_id"):
                        continue  # EAS active
                    trigger_mode = mapping.get("sports_trigger_mode", "always")
                    if trigger_mode in ("live_only", "favorites_only") and not mapping.get("sports_live"):
                        continue  # content gate not open
                    try:
                        from apps.channels.models import Channel as _Ch
                        from core.models import StreamProfile as _SP
                        channel = _Ch.objects.filter(id=cid).first()
                        orig    = _SP.objects.filter(id=mapping["original_profile_id"]).first()
                        if channel and orig:
                            position        = mapping.get("sports_position", "bottom")
                            fontsize        = int(mapping.get("sports_fontsize") or 36)
                            color_mode      = mapping.get("sports_color_mode", "single")
                            labelcolor      = mapping.get("sports_labelcolor", "#ffd700")
                            abbrcolor       = mapping.get("sports_abbrcolor",  "#00d4ff")
                            transcode_mode  = mapping.get("sports_transcode_mode_video", "1080p30")
                            ticker_style    = mapping.get("sports_ticker_style", "scrolling")
                            cloned, _ = _clone_and_inject_sports(cid, orig, channel.name, position, fontsize,
                                                                 labelcolor, abbrcolor, color_mode,
                                                                 transcode_mode, ticker_style)
                            _assign_profile(channel, cloned)
                            mapping["ticker_profile_id"] = cloned.id
                            mapping["sports_active"]     = True
                            mapping.pop("no_viewer_since", None)
                            mappings[str(cid)] = mapping
                            mappings_changed   = True
                            logger.info(f"ticker: sports on-demand overlay activated ch{cid} ({channel.name})")
                            _restart_channel_stream_async(channel, "Sports")
                    except Exception as e:
                        logger.warning(f"ticker: sports on-demand clone failed ch{cid}: {e}")

                if mappings_changed:
                    _save_mappings(mappings)
                    # Rebuild ch_list now that mappings changed
                    mappings = _get_mappings()
                    ch_list = _build_channel_list(mappings)
                    ch_ids = {ch[0]: ch for ch in ch_list}

                to_poll = [ch_ids[cid] for cid in newly_active if cid in ch_ids]
                if to_poll:
                    logger.info(f"ticker: stream-start → {[ch[2] for ch in to_poll]}")
                    _poll_channels(to_poll)
        except Exception as e:
            logger.debug(f"ticker: fast loop error: {e}")


def _channel_is_stale(channel_id, now):
    """True if the channel's song file is older than STALE_THRESHOLD seconds."""
    path = os.path.join(TICKER_DIR, f"channel_{channel_id}_song.txt")
    try:
        return (now - os.path.getmtime(path)) > STALE_THRESHOLD
    except OSError:
        return False  # file doesn't exist yet — channel hasn't been polled, not our problem to force


def _sweep_loop(stop_event):
    """Poll channels on a fixed interval. Gates to active channels when Redis is available.
    Also auto-recovers channels whose text files haven't been updated recently.
    For on-demand nowplaying channels, restores passthrough profile after _IDLE_RESTORE_DELAY
    seconds with no active viewers."""
    SWEEP_SLEEP = 15
    while not stop_event.is_set():
        rc = None
        try:
            rc = _get_redis_client()
            if rc is not None and not _redis_lock_acquire_or_refresh(rc, SWEEP_LOCK_KEY, _SWEEP_LOCK_TTL):
                stop_event.wait(timeout=SWEEP_SLEEP)
                continue
            mappings = _get_mappings()
            mappings_changed = False
            if mappings:
                # Only poll channels that have an active overlay profile
                all_ch = _build_channel_list(mappings)
                if all_ch:
                    active_ids = _redis_scan_active()
                    now = time.time()
                    if active_ids is not None:
                        active_ch = [c for c in all_ch if c[0] in active_ids]
                        stale_ch  = [c for c in all_ch
                                     if c[0] not in active_ids
                                     and _channel_is_stale(c[0], now)][:STALE_BATCH_SIZE]
                        ch_to_poll = active_ch + stale_ch
                        if ch_to_poll:
                            logger.debug(f"ticker: sweep {len(active_ch)} active, "
                                         f"{len(stale_ch)} stale (of {len(all_ch)})")
                            _poll_channels(ch_to_poll)

                        # On-demand nowplaying: restore passthrough when no viewers
                        try:
                            from apps.channels.models import Channel as _Ch
                            for cid_str, mapping in list(mappings.items()):
                                if mapping.get("type") != "nowplaying":
                                    continue
                                if mapping.get("np_trigger_mode", "on_demand") != "on_demand":
                                    continue
                                ticker_pid = mapping.get("ticker_profile_id")
                                if not ticker_pid:
                                    continue
                                try:
                                    cid_int = int(cid_str)
                                except (ValueError, TypeError):
                                    continue
                                if cid_int not in active_ids:
                                    no_viewer_since = mapping.get("no_viewer_since", 0)
                                    if not no_viewer_since:
                                        mapping["no_viewer_since"] = now
                                        mappings[cid_str] = mapping
                                        mappings_changed = True
                                    elif now - no_viewer_since >= _IDLE_RESTORE_DELAY:
                                        channel = _Ch.objects.filter(id=cid_int).first()
                                        if channel:
                                            _restore_profile(channel, mapping["original_profile_id"])
                                            _restart_channel_stream_async(channel, "NP")
                                        _delete_cloned_profile(ticker_pid)
                                        _remove_channel_files(cid_int)
                                        mapping["ticker_profile_id"] = None
                                        mapping.pop("no_viewer_since", None)
                                        mappings[cid_str] = mapping
                                        mappings_changed = True
                                        logger.info(f"ticker: on-demand idle-restore ch{cid_str} (no viewers >{_IDLE_RESTORE_DELAY}s)")
                                else:
                                    if "no_viewer_since" in mapping:
                                        mapping.pop("no_viewer_since")
                                        mappings[cid_str] = mapping
                                        mappings_changed = True
                        except Exception as e:
                            logger.warning(f"ticker: on-demand restore error: {e}")
                    else:
                        # Redis unavailable — fall back to all channels
                        logger.info(f"ticker: sweep (no Redis) — {len(all_ch)} channels")
                        _poll_channels(all_ch)

            if mappings_changed:
                _save_mappings(mappings)
        except Exception as e:
            logger.error(f"ticker: sweep error: {e}", exc_info=True)
        if rc is not None:
            try:
                _redis_lock_acquire_or_refresh(rc, SWEEP_LOCK_KEY, _SWEEP_LOCK_TTL)
            except Exception:
                pass
        stop_event.wait(timeout=SWEEP_SLEEP)


def _sports_sweep_loop(stop_event):
    """Poll ESPN every 30s and write scores to text files for active sports channels.
    For smart-mode channels (live_only / favorites_only), manages profile cloning/restoring.
    Auto-restores smart-mode channels to passthrough after _IDLE_RESTORE_DELAY seconds with no viewers."""
    SWEEP_SLEEP = 30
    while not stop_event.is_set():
        rc = None
        try:
            rc = _get_redis_client()
            if rc is not None and not _redis_lock_acquire_or_refresh(rc, SPORTS_LOCK_KEY, _SPORTS_LOCK_TTL):
                stop_event.wait(timeout=SWEEP_SLEEP)
                continue
            mappings = _get_mappings()
            sports_channels = [(cid, m) for cid, m in mappings.items() if m.get("type") == "sports"]
            mappings_changed = False

            if sports_channels:
                from apps.channels.models import Channel as _Channel
                from core.models import StreamProfile as _StreamProfile
                for cid, mapping in sports_channels:
                    sports_list  = mapping.get("sports_list", [])
                    favorites    = mapping.get("sports_favorites", "")
                    trigger_mode = mapping.get("sports_trigger_mode", "always")
                    if not sports_list:
                        continue
                    try:
                        if trigger_mode == "always":
                            labels, abbrevs, scores, full = _fetch_sports_text(sports_list, favorites)
                            if not scores:
                                placeholder = "  No games scheduled  "
                                pad         = " " * len(placeholder)
                                scores, labels, abbrevs, full = placeholder, pad, pad, placeholder
                            _write_sports_text(int(cid), labels, abbrevs, scores, full)
                        else:
                            fav_arg  = favorites if trigger_mode == "favorites_only" else ""
                            has_live = _has_live_games(sports_list, fav_arg)
                            was_live = mapping.get("sports_live", False)

                            if has_live:
                                # Write text files whenever games are live so data is
                                # ready the moment a viewer connects (fast_loop activates overlay)
                                labels, abbrevs, scores, full = _fetch_sports_text(sports_list, favorites)
                                if not scores:
                                    placeholder = "  No games scheduled  "
                                    pad         = " " * len(placeholder)
                                    scores, labels, abbrevs, full = placeholder, pad, pad, placeholder
                                _write_sports_text(int(cid), labels, abbrevs, scores, full)
                                if not was_live:
                                    mapping["sports_live"] = True
                                    mappings[cid]          = mapping
                                    mappings_changed       = True
                                    logger.info(f"ticker: sports live gate open ch{cid} (mode={trigger_mode})")

                            elif was_live:
                                # Games ended — close gate and restore profile if a viewer
                                # had triggered the overlay
                                ticker_pid = mapping.get("ticker_profile_id")
                                channel = _Channel.objects.filter(id=int(cid)).first()
                                if channel and ticker_pid:
                                    _restore_profile(channel, mapping["original_profile_id"])
                                    _restart_channel_stream_async(channel, "Sports")
                                if ticker_pid:
                                    _delete_cloned_profile(ticker_pid)
                                mapping["ticker_profile_id"] = None
                                mapping["sports_active"]     = False
                                mapping["sports_live"]       = False
                                mapping.pop("no_viewer_since", None)
                                mappings[cid]    = mapping
                                mappings_changed = True
                                _remove_channel_files(int(cid))
                                logger.info(f"ticker: sports live gate closed ch{cid} (no live games)")

                    except Exception as e:
                        logger.warning(f"ticker: sports write failed for channel {cid}: {e}")

            # Auto-restore smart-mode channels with no active viewers
            try:
                active_ids = _redis_scan_active()
                if active_ids is not None:
                    now = time.time()
                    from apps.channels.models import Channel as _Channel
                    for cid, mapping in list(mappings.items()):
                        if mapping.get("type") != "sports":
                            continue
                        ticker_pid = mapping.get("ticker_profile_id")
                        if not ticker_pid:
                            continue
                        if int(cid) not in active_ids:
                            no_viewer_since = mapping.get("no_viewer_since", 0)
                            if not no_viewer_since:
                                mapping["no_viewer_since"] = now
                                mappings[cid]    = mapping
                                mappings_changed = True
                            elif now - no_viewer_since >= _IDLE_RESTORE_DELAY:
                                channel = _Channel.objects.filter(id=int(cid)).first()
                                if channel:
                                    _restore_profile(channel, mapping["original_profile_id"])
                                _delete_cloned_profile(ticker_pid)
                                mapping["ticker_profile_id"] = None
                                mapping["sports_active"]     = False
                                mapping.pop("no_viewer_since", None)
                                mappings[cid]    = mapping
                                mappings_changed = True
                                logger.info(f"ticker: sports idle-restore ch{cid} (no viewers >{_IDLE_RESTORE_DELAY}s)")
                        else:
                            if "no_viewer_since" in mapping:
                                mapping.pop("no_viewer_since")
                                mappings[cid]    = mapping
                                mappings_changed = True
            except Exception as e:
                logger.warning(f"ticker: sports viewer auto-restore error: {e}")

            if mappings_changed:
                _save_mappings(mappings)

        except Exception as e:
            logger.error(f"ticker: sports sweep error: {e}", exc_info=True)
        finally:
            try:
                from django.db import connection
                connection.close()
            except Exception:
                pass
        if rc is not None:
            try:
                _redis_lock_acquire_or_refresh(rc, SPORTS_LOCK_KEY, _SPORTS_LOCK_TTL)
            except Exception:
                pass
        stop_event.wait(timeout=SWEEP_SLEEP)


_POLLER_LOCK_KEY = "ticker:poller:owner"
_POLLER_LOCK_TTL = 15  # seconds


def _try_acquire_poller_lock():
    """Cross-process leader election so only one Dispatcharr worker process runs
    Ticker's background loops. Without this, every uWSGI worker independently
    detects the same viewer connect and races to clone+restart the channel —
    confirmed in production logs as two overlapping restarts firing 5ms apart.
    Falls back to True (old per-worker behavior) if Redis is unreachable, since
    there's no way to coordinate without it anyway.
    """
    try:
        from apps.channels.models import RedisClient
        rc = RedisClient.get_client()
        return bool(rc.set(_POLLER_LOCK_KEY, str(os.getpid()), nx=True, ex=_POLLER_LOCK_TTL))
    except Exception:
        return True


def _renew_poller_lock(stop_event):
    """Keeps this worker's ownership alive. If the owning worker dies, the key
    expires and another worker acquires it on its next attempt (see caller)."""
    try:
        from apps.channels.models import RedisClient
        rc = RedisClient.get_client()
        token = str(os.getpid())
        while not stop_event.wait(timeout=_POLLER_LOCK_TTL / 3):
            try:
                rc.set(_POLLER_LOCK_KEY, token, xx=True, ex=_POLLER_LOCK_TTL)
            except Exception:
                pass
    except Exception:
        pass


def _poll_loop(stop_event):
    if not _try_acquire_poller_lock():
        logger.info(f"ticker: poller not started on PID {os.getpid()} — already owned by another worker")
        return
    logger.info(f"ticker: poller lock acquired by PID {os.getpid()}")
    renew_t = threading.Thread(target=_renew_poller_lock, args=(stop_event,), daemon=True)
    renew_t.start()

    try:
        _get_channel_data()
    except Exception:
        pass

    # Fast loop (Redis stream-start detection) runs in a sibling thread
    fast_t = threading.Thread(target=_fast_loop, args=(stop_event,), daemon=True)
    fast_t.start()
    # Sports sweep runs independently — 30s interval, ESPN scoreboard polling
    sports_t = threading.Thread(target=_sports_sweep_loop, args=(stop_event,), daemon=True)
    sports_t.start()
    # EAS sweep — NWS alert polling, interval configurable (default 60s)
    eas_t = threading.Thread(target=_eas_sweep_loop, args=(stop_event,), daemon=True)
    eas_t.start()
    # Now-playing sweep loop — one bulk fetch from ticker.com per cycle
    _sweep_loop(stop_event)

# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class Plugin:
    @property
    def fields(self):
        try:
            return self._build_fields()
        except Exception as e:
            logger.error(f"ticker: _build_fields failed: {e}", exc_info=True)
            return [{"id": "_error", "type": "info", "label": f"Ticker error: {e}"}]

    def __init__(self):
        global _scheduler_thread, _stop_event
        if _scheduler_thread is None or not _scheduler_thread.is_alive():
            _stop_event = threading.Event()
            _scheduler_thread = threading.Thread(
                target=_poll_loop,
                args=(_stop_event,),
                daemon=True,
                name="ticker-poller",
            )
            _scheduler_thread.start()
            logger.info("ticker: poller thread started")

    def _build_fields(self):
        try:
            from apps.channels.models import ChannelGroup, Channel
            managed_group_ids = set(
                Channel.objects.exclude(channel_group=None).values_list("channel_group_id", flat=True)
            )
            groups   = [{"value": str(g.id), "label": g.name}
                        for g in ChannelGroup.objects.filter(id__in=managed_group_ids).order_by("name")]
            channels = [{"value": str(c.id), "label": c.name}
                        for c in Channel.objects.exclude(channel_group=None).order_by("name")]
        except Exception:
            groups = []
            channels = []

        try:
            mappings = _get_mappings()
        except Exception:
            mappings = {}

        ticker_lines = []
        for cid, m in mappings.items():
            name = m.get("channel_name", f"Channel {cid}")
            ticker_type = m.get("type", "nowplaying")
            if ticker_type == "custom":
                style    = m.get("custom_style", "static")
                schedule = m.get("custom_schedule", "always")
                tag = f"{style}"
                if schedule == "timed":
                    tag += f", every {m.get('custom_interval', '?')}min"
                now_playing = f"[custom/{tag}] {m.get('custom_text', '')[:50]}"
            elif ticker_type == "sports":
                sl = m.get("sports_list", [])
                labels_str = ", ".join(LABELS.get(s, s) for s in sl[:5])
                fav = m.get("sports_favorites", "")
                now_playing = f"[sports: {labels_str}]" + (f" favs: {fav}" if fav else "")
            elif ticker_type == "eas":
                event = _eas_active.get(cid)
                now_playing = f"[EAS] ALERT: {event}" if event else "[EAS] monitoring - no active alert"
            else:
                artist_file = os.path.join(TICKER_DIR, f"channel_{cid}_artist.txt")
                song_file   = os.path.join(TICKER_DIR, f"channel_{cid}_song.txt")
                try:
                    with open(artist_file, encoding="utf-8") as f: artist = f.read().strip()
                    with open(song_file,   encoding="utf-8") as f: song   = f.read().strip()
                except Exception:
                    artist = song = ""
                now_playing = f"{artist} - {song}".strip(" -") if (artist or song) else "(no data yet)"
            ticker_lines.append(f"- {name}: {now_playing}")

        active_label = (f"{len(mappings)} active ticker(s):\n" + "\n".join(ticker_lines)) if mappings else "No active tickers."

        return [
            {"id": "_settings_save_warn", "type": "info", "label": "⚠ SAVING SETTINGS: Dispatcharr only writes this form to disk when you run an Action, not when you change a field and just close this dialog. After changing anything on this Settings tab, go to Actions and click Save Settings (or any other action) to be sure the change actually took effect — otherwise it silently reverts next time this loads."},
            # ── Now Playing ───────────────────────────────────────────────
            {"id": "_np_section",       "type": "info",   "label": "==========  NOW PLAYING  =========="},
            {"id": "np_target_type",    "type": "select", "label": "Apply To",
             "options": [{"value": "all", "label": "All Channels"}, {"value": "group", "label": "Channel Group"}, {"value": "groups", "label": "Multiple Groups (CSV)"}, {"value": "channel", "label": "Single Channel"}]},
            {"id": "_np_allchannels_warn", "type": "info", "label": "⚠ ALL CHANNELS WARNING: If you have other Ticker ticker types already active on specific channels or groups (e.g. EAS on news channels, Sports Ticker on a TV group), use Exclude Groups below to skip those groups. Channels already mapped to any ticker type are skipped automatically, but excluding groups avoids confusion and prevents unexpected results."},
            {"id": "_np_target_note",         "type": "info",   "label": "Fill in only the field that matches your Apply To selection above -- leave the others blank."},
            {"id": "np_channel_group_id",    "type": "select", "label": "Channel Group (Single Group)",             "options": groups},
            {"id": "np_channel_group_names", "type": "text",   "label": "Group Names (Multiple Groups -- comma-separated)", "placeholder": "e.g. Entertainment, Sports, News"},
            {"id": "np_channel_id",          "type": "select", "label": "Channel (Single Channel)",                 "options": channels},
            {"id": "np_exclude_groups",      "type": "text",   "label": "Exclude Groups (optional) — comma-separated group names to skip, e.g. News, Sports, Entertainment", "placeholder": "e.g. News, Sports TV"},
            {"id": "np_trigger_mode", "type": "select", "label": "Trigger Mode",
             "options": [
                 {"value": "on_demand", "label": "On-Demand (default) — overlay activates when a viewer tunes in, restores to passthrough when idle"},
                 {"value": "always",    "label": "Always On — overlay profile assigned permanently, no restart on connect (recommended for Plex or other players sensitive to mid-stream restarts)"},
             ]},
            # ── Satellite Radio Channel Setup ─────────────────────────────
            {"id": "_ch_section", "type": "info", "label": "==========  SATELLITE RADIO CHANNEL SETUP  =========="},
            {"id": "_ch_about",   "type": "info",
             "label": "Select a group or channel below, then use the Channel Setup actions to fill EPG, sort, or assign logos."},
            {"id": "ch_target_type", "type": "select", "label": "Apply To",
             "options": [{"value": "group", "label": "Channel Group"}, {"value": "channel", "label": "Single Channel"}]},
            {"id": "ch_channel_group_id", "type": "select", "label": "Channel Group", "options": groups},
            {"id": "ch_channel_id",       "type": "select", "label": "Channel",       "options": channels},
            {"id": "sort_start_number",   "type": "text",   "label": "Sort Start Number",
             "placeholder": "Leave blank to auto-detect from current channel numbers"},
            {"id": "sort_numbering_mode", "type": "select", "label": "Numbering Mode",
             "options": [
                 {"value": "sequential", "label": "Sequential (default) — channels packed with no gaps, e.g. start 15000 -> 15000, 15001, 15002..."},
                 {"value": "absolute",   "label": "Absolute — channel number = Sort Start Number + SXM station number, e.g. start 15000 + station 036 -> 15036 (preserves gaps from missing stations)"},
             ], "help_text": "Absolute mode needs an explicit Sort Start Number every run — auto-detect reads the current minimum channel number, which drifts once channels carry absolute (gapped) numbers."},
            {"id": "sort_block_size", "type": "text", "label": "Numbering Block Size (required for Absolute mode)",
             "placeholder": "e.g. 1000",
             "help_text": (
                 "Reserves Sort Start Number through Start+BlockSize-1 for this group, so unmatched "
                 "channels never drift into whatever you numbered next. Required when Numbering Mode "
                 "is Absolute — Sort Channels refuses to run without it, since a silent default risks "
                 "overlapping a neighboring channel group. SiriusXM's full catalog runs up to station "
                 "1999, but a real 431-channel provider lineup we checked had 99.8% of channels at or "
                 "below 999 (one rare event-channel outlier near 1999) — 1000 comfortably covers a "
                 "typical lineup with no overflow. A matched channel whose real station number falls "
                 "outside the block (like that outlier) still gets its correct absolute number and is "
                 "called out in the result message, never silently renumbered."
             )},
            {"id": "sort_station_prefix", "type": "boolean", "label": "Prefix Channel Name With Station Number",
             "help_text": "Renames each channel with its SXM station number as a prefix, format controlled below. Applied whenever Sort Channels runs."},
            {"id": "sort_station_prefix_format", "type": "select", "label": "Station Number Prefix Format",
             "options": [
                 {"value": "zero_padded", "label": "Zero-Padded (036 Alt Nation) — minimum 3 digits, never truncated for 4+ digit stations (e.g. 1285 Southern Rock)"},
                 {"value": "no_padding",  "label": "No Leading Zeros (36 Alt Nation)"},
             ], "help_text": "Only used when Prefix Channel Name With Station Number is on."},
            # ── EAS ───────────────────────────────────────────────────────
            {"id": "_eas_section",  "type": "info", "label": "==========  EAS/JAS WEATHER ALERTS  =========="},
            {"id": "_eas_tribute",  "type": "info",
             "label": "JAS = jesmannstl Alert System. Dedicated to jesmannstl -- a weather fanatic and beloved member of the Dispatcharr community. Every alert that fires is a reminder of him. Rest in peace."},
            {"id": "_eas_about",    "type": "info",
             "label": "Monitors NWS (USA) and Environment Canada alerts for configured zones and cities. When an alert fires, affected channels automatically switch to an EAS overlay profile and restart. Channels return to normal passthrough when the alert clears."},
            {"id": "_eas_transcode_note", "type": "info",
             "label": "⚠ TRANSCODING NOTE (active alerts only): EAS overlays require FFmpeg to decode and re-encode video while an alert is active. Channels return to their original profile automatically when the alert clears — transcoding only occurs during the alert itself. If you experience buffering or stuttering during an alert, lower the Transcode Quality setting below."},
            {"id": "eas_transcode_mode", "type": "select", "label": "EAS Transcode Quality",
             "options": [
                 {"value": "full",    "label": "Full quality — source resolution and framerate (default)"},
                 {"value": "1080p30", "label": "1080p 30fps — full resolution, framerate capped at 30fps"},
                 {"value": "720p",    "label": "720p — scaled to 720p at source framerate"},
                 {"value": "720p30",  "label": "720p 30fps — scaled to 720p, capped at 30fps (maximum CPU reduction)"},
             ]},
            # ── USA NWS ──
            {"id": "_eas_usa_section", "type": "info", "label": "---------- USA — NWS Alerts ----------"},
            {"id": "_eas_zone_help", "type": "info",
             "label": "Enter your NWS zone or county codes below. Use the Zone Lookup action to search by US state — it will return all matching zone codes and names."},
            {"id": "eas_zones",     "type": "text",   "label": "NWS Alert Zones — USA (Active — triggers alerts)",
             "placeholder": "e.g. TXC113,TXC121"},
            {"id": "eas_zone_lookup_state", "type": "text", "label": "Zone Lookup — US State Code (lookup only, not saved)",
             "placeholder": "e.g. TX, WA, FL, NY"},
            {"id": "eas_target_type", "type": "select", "label": "USA — Apply To",
             "options": [{"value": "all", "label": "All Channels"}, {"value": "group", "label": "Channel Group"}, {"value": "groups", "label": "Multiple Groups (CSV)"}, {"value": "channel", "label": "Single Channel"}]},
            {"id": "_eas_allchannels_warn", "type": "info", "label": "ℹ EAS CO-ARM: EAS can be enabled alongside any other ticker type (Now Playing, Custom Text, Sports). Channels with existing tickers will have EAS armed on top — they continue running normally and only switch to the EAS overlay when a real alert fires."},
            {"id": "_eas_target_note", "type": "info", "label": "Fill in only the field that matches your USA Apply To selection above — leave the others blank."},
            {"id": "eas_channel_group_id",    "type": "select", "label": "USA — Channel Group (Single Group)",              "options": groups},
            {"id": "eas_channel_group_names", "type": "text",   "label": "USA — Group Names (Multiple Groups — comma-separated)", "placeholder": "e.g. Entertainment, Sports, News"},
            {"id": "eas_channel_id",          "type": "select", "label": "USA — Channel (Single Channel)",                  "options": channels},
            {"id": "eas_exclude_groups",      "type": "text",   "label": "USA — Exclude Groups (optional)",                 "placeholder": "e.g. SiriusXM, Satellite Radio"},
            # ── Canada EC ──
            {"id": "_eas_ca_section", "type": "info", "label": "---------- Canada — Environment Canada Alerts ----------"},
            {"id": "_eas_ca_help", "type": "info",
             "label": "Enter your Environment Canada city IDs below. Use the City Lookup action to search by city name or province code. Both USA and Canada can be active simultaneously on independent channel groups."},
            {"id": "eas_ca_city_ids", "type": "text",   "label": "Weather Canada City IDs — Canada (Active — triggers alerts)",
             "placeholder": "e.g. on-143 (Toronto), ns-19 (Halifax)"},
            {"id": "eas_ca_lookup_query", "type": "text", "label": "City Lookup — City Name or Province Code (lookup only, not saved)",
             "placeholder": "e.g. Toronto  or  ON  or  Montréal"},
            {"id": "eas_ca_target_type", "type": "select", "label": "Canada — Apply To",
             "options": [{"value": "all", "label": "All Channels"}, {"value": "group", "label": "Channel Group"}, {"value": "groups", "label": "Multiple Groups (CSV)"}, {"value": "channel", "label": "Single Channel"}]},
            {"id": "_eas_ca_target_note", "type": "info", "label": "Fill in only the field that matches your Canada Apply To selection above — leave the others blank."},
            {"id": "eas_ca_channel_group_id",    "type": "select", "label": "Canada — Channel Group (Single Group)",              "options": groups},
            {"id": "eas_ca_channel_group_names", "type": "text",   "label": "Canada — Group Names (Multiple Groups — comma-separated)", "placeholder": "e.g. News, Local"},
            {"id": "eas_ca_channel_id",          "type": "select", "label": "Canada — Channel (Single Channel)",                  "options": channels},
            {"id": "eas_ca_exclude_groups",      "type": "text",   "label": "Canada — Exclude Groups (optional)",                 "placeholder": "e.g. SiriusXM, Satellite Radio"},
            {"id": "eas_ca_language", "type": "select", "label": "Canada — Alert Language",
             "options": [
                 {"value": "en",   "label": "English"},
                 {"value": "fr",   "label": "French"},
                 {"value": "both", "label": "Both (English / French)"},
             ],
             "default": "en"},
            # ── Shared Settings ──
            {"id": "_eas_shared_section", "type": "info", "label": "---------- Shared Settings (USA + Canada) ----------"},
            {"id": "eas_severity_filter", "type": "select", "label": "Minimum Severity — USA: Watch/Warning/Emergency · Canada: Yellow/Orange/Red",
             "options": [
                 {"value": "Moderate", "label": "Watch / Yellow (Moderate and above)"},
                 {"value": "Severe",   "label": "Warning / Orange (Severe and above)"},
                 {"value": "Extreme",  "label": "Emergency / Red (Extreme only)"},
             ]},
            {"id": "eas_overlay_style", "type": "select", "label": "Alert Overlay Style",
             "options": [
                 {"value": "broadcast", "label": "TV Broadcast (news ticker bar — label box + scrolling crawl)"},
                 {"value": "ticker",   "label": "Ticker Custom (flashing overlay boxes)"},
             ]},
            {"id": "eas_poll_interval",  "type": "number", "label": "Poll Interval (seconds)", "min": 15},
            {"id": "eas_tone_interval",  "type": "number", "label": "Siren Tone Interval (seconds) — how often the attention tone repeats during an active alert. Set to 0 to disable the tone entirely.", "min": 0},
            {"id": "eas_test_duration",  "type": "number", "label": "Test Alert Duration (seconds) — how long Test Alert fires before auto-restoring (default 60)", "min": 10},
            {"id": "_eas_saved_help", "type": "info",
             "label": "Saved Codes (reference only — paste commonly-used codes here so you do not have to look them up each time; not monitored):"},
            {"id": "eas_saved_codes", "type": "text", "label": "Saved / Favorite Codes",
             "placeholder": "e.g. OKC111,OKC037,WAZ702"},
            # ── Custom Text ───────────────────────────────────────────────
            {"id": "_custom_section",      "type": "info",   "label": "==========  CUSTOM TEXT  =========="},
            {"id": "_custom_transcode_note", "type": "info",
             "label": "⚠ TRANSCODING NOTE (ticker active only): Custom text overlays require FFmpeg to decode and re-encode video while the ticker is running. Channels on stream-copy profiles will transcode only while the custom ticker is active — they return to their original profile when the ticker is disabled. If you experience buffering or stuttering, your system may not have sufficient CPU headroom for transcoding at your source resolution and framerate."},
            {"id": "custom_target_type",   "type": "select", "label": "Apply To",
             "options": [{"value": "all", "label": "All Channels"}, {"value": "group", "label": "Channel Group"}, {"value": "groups", "label": "Multiple Groups (CSV)"}, {"value": "channel", "label": "Single Channel"}]},
            {"id": "_custom_allchannels_warn", "type": "info", "label": "⚠ ALL CHANNELS WARNING: If you have other Ticker ticker types already active on specific channels or groups (e.g. Now Playing on satellite radio, EAS on news channels), use Exclude Groups below to skip those groups. Channels already mapped to any ticker type are skipped automatically, but excluding groups avoids confusion and prevents unexpected results."},
            {"id": "_custom_target_note",         "type": "info",   "label": "Fill in only the field that matches your Apply To selection above -- leave the others blank."},
            {"id": "custom_channel_group_id",    "type": "select", "label": "Channel Group (Single Group)",             "options": groups},
            {"id": "custom_channel_group_names", "type": "text",   "label": "Group Names (Multiple Groups -- comma-separated)", "placeholder": "e.g. Entertainment, Sports, News"},
            {"id": "custom_channel_id",          "type": "select", "label": "Channel (Single Channel)",                 "options": channels},
            {"id": "custom_exclude_groups",      "type": "text",   "label": "Exclude Groups (optional) — comma-separated group names to skip, e.g. SiriusXM, News", "placeholder": "e.g. SiriusXM, News"},
            {"id": "custom_trigger_mode", "type": "select", "label": "Trigger Mode",
             "options": [
                 {"value": "on_demand", "label": "On-Demand (default) — overlay activates when text is set, restores to passthrough when text is cleared"},
                 {"value": "always",    "label": "Always On — overlay encodes 24/7 (legacy behavior)"},
             ]},
            {"id": "custom_text",      "type": "text",   "label": "Custom Text (leave blank in On-Demand mode to register the channel now and set text later)"},
            {"id": "custom_style",     "type": "select", "label": "Style",
             "options": [{"value": "static", "label": "Static"}, {"value": "scrolling", "label": "Scrolling"}]},
            {"id": "custom_position",  "type": "select", "label": "Position",
             "options": [
                 {"value": "bottom", "label": "Bottom"},
                 {"value": "top",    "label": "Top"},
                 {"value": "center", "label": "Center"},
             ]},
            {"id": "custom_schedule",  "type": "select", "label": "Schedule",
             "options": [{"value": "always", "label": "Always On"}, {"value": "timed", "label": "Timed"}]},
            {"id": "custom_duration",  "type": "number", "label": "Display Duration (seconds) - Timed only"},
            {"id": "custom_interval",  "type": "number", "label": "Repeat Interval (minutes) - Timed only"},
            # ── Sports Ticker ─────────────────────────────────────────────
            {"id": "_sports_section",       "type": "info",    "label": "==========  SPORTS TICKER  =========="},
            {"id": "_sports_transcode_note", "type": "info",
             "label": "⚠ TRANSCODING NOTE (ticker active only): Sports score overlays require FFmpeg to decode and re-encode video while the ticker is running. Channels on stream-copy profiles will transcode only while the sports ticker is active — they return to their original profile when the ticker is disabled. High-framerate sources (59.94fps) use approximately twice the CPU of standard sources (29.97fps) — lower the Transcode Quality if you experience buffering."},
            {"id": "sports_transcode_mode_video", "type": "select", "label": "Transcode Quality (Video Channels)",
             "options": [
                 {"value": "1080p30", "label": "1080p 30fps — full resolution, framerate capped at 30fps (default; recommended for most setups)"},
                 {"value": "full",    "label": "Full quality — source resolution and framerate (best quality; requires capable CPU)"},
                 {"value": "720p",    "label": "720p — scaled to 720p at source framerate (significant CPU reduction)"},
                 {"value": "720p30",  "label": "720p 30fps — scaled to 720p, capped at 30fps (maximum CPU reduction)"},
             ]},
            {"id": "_sports_football",      "type": "info",    "label": "-- Football --"},
            {"id": "sports_nfl",            "type": "boolean", "label": "NFL"},
            {"id": "sports_ncaafb",         "type": "boolean", "label": "College Football (NCAAF)"},
            {"id": "sports_cfl",            "type": "boolean", "label": "CFL"},
            {"id": "sports_ufl",            "type": "boolean", "label": "UFL"},
            {"id": "sports_xfl",            "type": "boolean", "label": "XFL"},
            {"id": "_sports_basketball",    "type": "info",    "label": "-- Basketball --"},
            {"id": "sports_nba",            "type": "boolean", "label": "NBA"},
            {"id": "sports_wnba",           "type": "boolean", "label": "WNBA"},
            {"id": "sports_ncaamb",         "type": "boolean", "label": "NCAA Men's Basketball"},
            {"id": "sports_ncaawb",         "type": "boolean", "label": "NCAA Women's Basketball"},
            {"id": "sports_nbagl",          "type": "boolean", "label": "NBA G League"},
            {"id": "_sports_baseball",      "type": "info",    "label": "-- Baseball / Softball --"},
            {"id": "sports_mlb",            "type": "boolean", "label": "MLB"},
            {"id": "sports_ncaabase",       "type": "boolean", "label": "NCAA Baseball"},
            {"id": "sports_ncaasb",         "type": "boolean", "label": "NCAA Softball"},
            {"id": "_sports_hockey",        "type": "info",    "label": "-- Hockey --"},
            {"id": "sports_nhl",            "type": "boolean", "label": "NHL"},
            {"id": "sports_ncaahm",         "type": "boolean", "label": "NCAA Men's Hockey"},
            {"id": "sports_ncaahw",         "type": "boolean", "label": "NCAA Women's Hockey"},
            {"id": "_sports_soccer_na",     "type": "info",    "label": "-- Soccer — North America --"},
            {"id": "sports_mls",            "type": "boolean", "label": "MLS"},
            {"id": "sports_nwsl",           "type": "boolean", "label": "NWSL"},
            {"id": "sports_ligamx",         "type": "boolean", "label": "Liga MX"},
            {"id": "_sports_soccer_eu",     "type": "info",    "label": "-- Soccer — Europe (Big 5) --"},
            {"id": "sports_epl",            "type": "boolean", "label": "EPL (English Premier League)"},
            {"id": "sports_laliga",         "type": "boolean", "label": "La Liga"},
            {"id": "sports_bundesliga",     "type": "boolean", "label": "Bundesliga"},
            {"id": "sports_seriea",         "type": "boolean", "label": "Serie A"},
            {"id": "sports_ligue1",         "type": "boolean", "label": "Ligue 1"},
            {"id": "_sports_soccer_eu2",    "type": "info",    "label": "-- Soccer — Europe (Other) --"},
            {"id": "sports_eredivisie",     "type": "boolean", "label": "Eredivisie (Netherlands)"},
            {"id": "sports_primlg",         "type": "boolean", "label": "Primeira Liga (Portugal)"},
            {"id": "sports_sco1",           "type": "boolean", "label": "Scottish Premiership"},
            {"id": "_sports_soccer_uefa",   "type": "info",    "label": "-- Soccer — UEFA --"},
            {"id": "sports_ucl",            "type": "boolean", "label": "UEFA Champions League"},
            {"id": "sports_uel",            "type": "boolean", "label": "UEFA Europa League"},
            {"id": "sports_uecl",           "type": "boolean", "label": "UEFA Conference League"},
            {"id": "_sports_soccer_intl",   "type": "info",    "label": "-- Soccer — International --"},
            {"id": "sports_fifawc",         "type": "boolean", "label": "FIFA World Cup"},
            {"id": "sports_fifawwc",        "type": "boolean", "label": "FIFA Women's World Cup"},
            {"id": "sports_copa",           "type": "boolean", "label": "Copa America"},
            {"id": "sports_concacaf",       "type": "boolean", "label": "CONCACAF Champions Cup"},
            {"id": "sports_lgscup",         "type": "boolean", "label": "Leagues Cup (MLS/Liga MX)"},
            {"id": "_sports_golf",          "type": "info",    "label": "-- Golf --"},
            {"id": "sports_pga",            "type": "boolean", "label": "PGA Tour"},
            {"id": "sports_lpga",           "type": "boolean", "label": "LPGA"},
            {"id": "sports_dpwt",           "type": "boolean", "label": "DP World Tour (European Tour)"},
            {"id": "sports_liv",            "type": "boolean", "label": "LIV Golf"},
            {"id": "sports_pgachamp",       "type": "boolean", "label": "PGA Champions Tour"},
            {"id": "_sports_motor",         "type": "info",    "label": "-- Motorsports --"},
            {"id": "sports_f1",             "type": "boolean", "label": "Formula 1"},
            {"id": "sports_indycar",        "type": "boolean", "label": "IndyCar"},
            {"id": "sports_nascar",         "type": "boolean", "label": "NASCAR Cup Series (live races only)"},
            {"id": "_sports_combat",        "type": "info",    "label": "-- Combat Sports --"},
            {"id": "sports_ufc",            "type": "boolean", "label": "UFC / MMA"},
            {"id": "_sports_tennis",        "type": "info",    "label": "-- Tennis --"},
            {"id": "sports_atp",            "type": "boolean", "label": "ATP Tour"},
            {"id": "sports_wta",            "type": "boolean", "label": "WTA Tour"},
            {"id": "_sports_college_other", "type": "info",    "label": "-- College & Other --"},
            {"id": "sports_ncaavb",         "type": "boolean", "label": "NCAA Volleyball"},
            {"id": "sports_ncaalaxm",       "type": "boolean", "label": "NCAA Men's Lacrosse"},
            {"id": "sports_ncaalaxw",       "type": "boolean", "label": "NCAA Women's Lacrosse"},
            {"id": "sports_pll",            "type": "boolean", "label": "PLL (Premier Lacrosse League)"},
            {"id": "sports_favorites",      "type": "text",    "label": "Favorite Teams (abbreviations, comma-separated - blank = all teams)"},
            {"id": "sports_trigger_mode",   "type": "select",  "label": "Trigger Mode",
             "options": [
                 {"value": "always",         "label": "Always On — ticker encodes 24/7 (default)"},
                 {"value": "live_only",      "label": "Active Games Only — fires only when a live game is in progress"},
                 {"value": "favorites_only", "label": "Favorite Teams Only — fires only when a favorite team is playing (requires Favorite Teams above)"},
             ]},
            {"id": "sports_color_mode",     "type": "select",  "label": "Color Mode",
             "options": [
                 {"value": "single", "label": "Single Color - White (recommended, lower CPU)"},
                 {"value": "multi",  "label": "Multi-Color - Sport labels + team abbreviations colored"},
             ]},
            {"id": "sports_position",       "type": "select",  "label": "Ticker Position",
             "options": [{"value": "bottom", "label": "Bottom"}, {"value": "top", "label": "Top"}]},
            {"id": "sports_fontsize",       "type": "number",  "label": "Font Size (default 36)", "min": 16},
            {"id": "sports_ticker_static",  "type": "boolean", "label": "Static Ticker — centered and fixed (default: scrolling; enable when using Favorite Teams with 1–2 teams)"},
            {"id": "sports_labelcolor",     "type": "select",  "label": "Sport Label Color (NFL:, NBA:, etc.) - Multi-Color only",
             "options": [
                 {"value": "#ffd700", "label": "Gold (default)"},
                 {"value": "#00d4ff", "label": "Cyan"},
                 {"value": "#ff8c00", "label": "Orange"},
                 {"value": "#00ff80", "label": "Green"},
                 {"value": "#ff4444", "label": "Red"},
                 {"value": "#ffffff", "label": "White (no distinction)"},
             ]},
            {"id": "sports_abbrcolor",      "type": "select",  "label": "Team Abbreviation Color (KC, DEN, etc.) - Multi-Color only",
             "options": [
                 {"value": "#00d4ff", "label": "Cyan (default)"},
                 {"value": "#ffd700", "label": "Gold"},
                 {"value": "#ff8c00", "label": "Orange"},
                 {"value": "#00ff80", "label": "Green"},
                 {"value": "#ff4444", "label": "Red"},
                 {"value": "#ffffff", "label": "White (no distinction)"},
             ]},
            {"id": "sports_target_type",    "type": "select",  "label": "Apply To",
             "options": [{"value": "all", "label": "All Channels"}, {"value": "group", "label": "Channel Group"}, {"value": "groups", "label": "Multiple Groups (CSV)"}, {"value": "channel", "label": "Single Channel"}]},
            {"id": "_sports_allchannels_warn", "type": "info", "label": "⚠ ALL CHANNELS WARNING: If you have other Ticker ticker types already active on specific channels or groups (e.g. Now Playing on satellite radio, EAS on news channels), use Exclude Groups below to skip those groups. Channels already mapped to any ticker type are skipped automatically, but excluding groups avoids confusion and prevents unexpected results."},
            {"id": "_sports_target_note",         "type": "info",   "label": "Fill in only the field that matches your Apply To selection above -- leave the others blank."},
            {"id": "sports_channel_group_id",    "type": "select", "label": "Channel Group (Single Group)",             "options": groups},
            {"id": "sports_channel_group_names", "type": "text",   "label": "Group Names (Multiple Groups -- comma-separated)", "placeholder": "e.g. Entertainment, Sports, News"},
            {"id": "sports_channel_id",          "type": "select", "label": "Channel (Single Channel)",                 "options": channels},
            {"id": "sports_exclude_groups",      "type": "text",   "label": "Exclude Groups (optional) — comma-separated group names to skip, e.g. SiriusXM, News", "placeholder": "e.g. SiriusXM, News"},
            {"id": "sports_test_duration",       "type": "number", "label": "Test Ticker Duration (seconds) — how long the Test Sports Ticker action runs before auto-restoring (default 60)", "min": 10},
            # ── Active Tickers ────────────────────────────────────────────
            {"id": "_active_section", "type": "info", "label": "==========  ACTIVE TICKERS  =========="},
            {"id": "_ticker_list",    "type": "info", "label": active_label},
        ]

    def run(self, action, params, context):
        saved = _get_settings()
        base = {k: v for k, v in saved.items() if k not in ("channel_mappings", "channel_cache")}
        if params:
            base.update(params)
        params = base

        dispatch = {
            "enable_nowplaying":    self._enable_nowplaying,
            "disable_nowplaying":   self._disable_nowplaying,
            "enable_custom":        self._enable_custom,
            "update_custom":        self._update_custom,
            "disable_custom":       self._disable_custom_ticker,
            "enable_sports":        self._enable_sports,
            "update_sports":        self._update_sports,
            "test_sports":          self._test_sports,
            "disable_sports":       self._disable_sports_ticker,
            "enable_eas":           self._enable_eas,
            "disable_eas":          self._disable_eas,
            "test_eas":             self._test_eas,
            "migrate_eas":          self._migrate_eas,
            "lookup_eas_zones":      self._lookup_eas_zones,
            "lookup_eas_ca":         self._lookup_eas_ca,
            "enable_eas_ca":        self._enable_eas_ca,
            "disable_eas_ca":       self._disable_eas_ca,
            "test_eas_ca":          self._test_eas_ca,
            "disable_all":          self._disable_all,
            "save_settings":        self._save_settings,
            "view_active":          self._view_active,
            "refresh_channels":     self._refresh_channels,
            "fill_sxm_epg":         self._fill_sxm_epg,
            "fill_epg":             self._fill_epg,
            "sort_channels":        self._sort_channels,
            "assign_logos":         self._assign_logos,
            "fill_and_sort":        self._fill_and_sort,
            "fill_sort_logos":      self._fill_sort_logos,
            "clean_orphans":        self._clean_orphans,
            "migrate_shared_profiles": self._migrate_shared_profiles,
            "migrate_from_tickarr":  self._migrate_from_tickarr,
            "check_channel_id_support": self._check_channel_id_support,
            "redis_diag":           self._redis_diag,
            "reload_poller":        self._reload_poller,
            "restart_dispatcharr":  self._restart_dispatcharr,
        }
        if action.startswith("_section_"):
            return {"success": True, "message": ""}
        handler = dispatch.get(action)
        if not handler:
            return {"success": False, "message": f"Unknown action: {action}"}
        try:
            return handler(params)
        except Exception as e:
            logger.error(f"ticker: action {action} failed: {e}", exc_info=True)
            return {"success": False, "message": f"Error: {e}"}

    def stop(self, context):
        global _stop_event, _scheduler_thread
        _stop_event.set()
        if _scheduler_thread:
            _scheduler_thread.join(timeout=5)
        logger.info("ticker: poller thread stopped")

    # ------------------------------------------------------------------ #
    # Actions                                                              #
    # ------------------------------------------------------------------ #

    def _enable_nowplaying(self, params):
        from apps.channels.models import Channel, ChannelGroup

        trigger_mode = (params.get("np_trigger_mode") or "on_demand").strip()

        channels = self._resolve_channels(params, prefix="np_")
        if not channels:
            return {"success": False, "message": "No channels found for the selected target."}

        xm_channels, aliases = _get_channel_data()
        stations = _get_stations()
        mappings = _get_mappings()

        enabled, skipped, failed = [], [], []

        for channel in channels:
            cid = str(channel.id)
            if cid in mappings:
                skipped.append(f"{channel.name} (already enabled)")
                continue
            try:
                original_profile = channel.stream_profile
                if not original_profile:
                    try:
                        original_profile = channel.get_stream_profile()
                    except Exception:
                        original_profile = None
                if not original_profile:
                    skipped.append(f"{channel.name} (no stream profile assigned — go to Channels, open this channel, and assign any stream profile, then re-run)")
                    continue
                if not (original_profile.parameters or "").strip():
                    skipped.append(f"{channel.name} (stream profile is Proxy or Redirect — assign an FFmpeg profile to enable this ticker)")
                    continue
                if original_profile.name.startswith(PROFILE_PREFIX):
                    skipped.append(f"{channel.name} (already has a Ticker profile — run Disable Ticker first)")
                    continue

                # Strip any station-number prefix Sort may have already added — otherwise
                # a channel named "036 Alt Nation" fails to match "Alt Nation" here.
                match_name = re.sub(r'^\d+\s+', '', channel.name)
                xm_entry = _match_channel(match_name, xm_channels, aliases)
                deeplink = None
                channel_description = ""
                if xm_entry:
                    channel_description = xm_entry.get("description", "")
                    uuid = xm_entry.get("lookaround_channel_id")
                    station = (_match_station_by_uuid(uuid, stations) if uuid else None) or \
                              _match_station_by_name(match_name, stations)
                else:
                    station = _match_station_by_name(channel.name, stations)
                if station:
                    deeplink = station.get("id")

                removed_flags = []
                ticker_profile_id = None

                if trigger_mode == "always":
                    cloned, removed_flags = _clone_and_inject(channel.id, original_profile, channel.name)
                    _assign_profile(channel, cloned)
                    ticker_profile_id = cloned.id
                    _write_fallback(channel.id, channel.name, channel_description)

                mappings[cid] = {
                    "original_profile_id": original_profile.id,
                    "ticker_profile_id":   ticker_profile_id,
                    "xm_deeplink":         deeplink,
                    "channel_name":        channel.name,
                    "channel_description": channel_description,
                    "type":                "nowplaying",
                    "np_trigger_mode":     trigger_mode,
                }

                note = (f" [auto-removed from cloned profile: {', '.join(removed_flags)}]"
                        if removed_flags else "")
                enabled.append(f"{channel.name}{note}")
            except Exception as e:
                logger.error(f"ticker: enable failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} (error: {e})")

        _save_mappings(mappings)

        trigger_note = (
            "Overlay will activate automatically when a viewer connects and restore to passthrough when idle."
            if trigger_mode == "on_demand" else
            "Overlay will encode 24/7 (always-on mode)."
        )
        parts = []
        if enabled:  parts.append(f"Enabled: {len(enabled)} channel(s)\n{trigger_note}")
        if skipped:  parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:   parts.append("Failed:\n"  + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _enable_custom(self, params):
        from apps.channels.models import Channel, ChannelGroup

        trigger_mode = (params.get("custom_trigger_mode") or "on_demand").strip()
        custom_text  = (params.get("custom_text") or "").strip()

        if trigger_mode == "always" and not custom_text:
            return {"success": False, "message": "Custom Text is required for Always On mode."}

        style    = params.get("custom_style",    "static")
        position = params.get("custom_position", "bottom")
        schedule = params.get("custom_schedule", "always")

        try:
            duration = int(params.get("custom_duration") or 10)
            interval = int(params.get("custom_interval") or 5)
        except (ValueError, TypeError):
            return {"success": False, "message": "Duration and Interval must be integers."}

        if schedule == "timed":
            if duration <= 0:
                return {"success": False, "message": "Display Duration must be greater than 0."}
            if interval <= 0:
                return {"success": False, "message": "Repeat Interval must be greater than 0."}
            if duration >= interval * 60:
                return {"success": False, "message": "Display Duration (seconds) must be less than Repeat Interval (minutes x 60)."}

        channels = self._resolve_channels(params, prefix="custom_")
        if not channels:
            return {"success": False, "message": "No channels found for the selected target."}

        mappings = _get_mappings()
        enabled, skipped, failed = [], [], []

        for channel in channels:
            cid = str(channel.id)
            if cid in mappings:
                existing_type = mappings[cid].get("type", "nowplaying")
                skipped.append(f"{channel.name} (already has a {existing_type} ticker — disable first)")
                continue
            try:
                original_profile = channel.stream_profile
                if not original_profile:
                    try:
                        original_profile = channel.get_stream_profile()
                    except Exception:
                        original_profile = None
                if not original_profile:
                    skipped.append(f"{channel.name} (no stream profile assigned — go to Channels, open this channel, and assign any stream profile, then re-run)")
                    continue
                if not (original_profile.parameters or "").strip():
                    skipped.append(f"{channel.name} (stream profile is Proxy or Redirect — assign an FFmpeg profile to enable this ticker)")
                    continue
                if original_profile.name.startswith(PROFILE_PREFIX):
                    skipped.append(f"{channel.name} (already has a Ticker profile — run Disable Ticker first)")
                    continue

                removed_flags = []
                ticker_profile_id = None

                if trigger_mode == "always" or (trigger_mode == "on_demand" and custom_text):
                    cloned, removed_flags = _clone_and_inject_custom(
                        channel.id, original_profile, channel.name, style, position, schedule, duration, interval
                    )
                    _assign_profile(channel, cloned)
                    ticker_profile_id = cloned.id
                    _write_custom_text(channel.id, custom_text)

                mappings[cid] = {
                    "original_profile_id":  original_profile.id,
                    "ticker_profile_id":    ticker_profile_id,
                    "channel_name":         channel.name,
                    "type":                 "custom",
                    "custom_text":          custom_text,
                    "custom_style":         style,
                    "custom_position":      position,
                    "custom_schedule":      schedule,
                    "custom_duration":      duration,
                    "custom_interval":      interval,
                    "custom_trigger_mode":  trigger_mode,
                }
                note = (f" [auto-removed: {', '.join(removed_flags)}]" if removed_flags else "")
                enabled.append(f"{channel.name}{note}")

            except Exception as e:
                logger.error(f"ticker: enable_custom failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} (error: {e})")

        _save_mappings(mappings)

        trigger_note = (
            "Overlay will activate when text is set via Update Custom Text, and restore when text is cleared."
            if trigger_mode == "on_demand" else
            "Overlay will encode 24/7 (always-on mode)."
        )
        parts = []
        if enabled: parts.append(f"Enabled: {len(enabled)} channel(s)\n{trigger_note}")
        if skipped: parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:  parts.append("Failed:\n"  + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _update_custom(self, params):
        custom_text = (params.get("custom_text") or "").strip()

        channels = (
            self._resolve_channels(params, prefix="custom_") or
            self._resolve_channels(params, prefix="np_")
        )
        if not channels:
            return {"success": False, "message": "No channels found. Select a channel in the Custom Text section."}

        mappings = _get_mappings()
        mappings_changed = False
        updated, cleared, skipped = [], [], []

        for channel in channels:
            cid = str(channel.id)
            mapping = mappings.get(cid)
            if not mapping or mapping.get("type") != "custom":
                skipped.append(f"{channel.name} (no custom ticker active — enable it first)")
                continue

            trigger_mode   = mapping.get("custom_trigger_mode", "always")
            ticker_pid     = mapping.get("ticker_profile_id")

            if trigger_mode == "always":
                if not custom_text:
                    skipped.append(f"{channel.name} (text required for Always On mode — use Disable to remove the overlay)")
                    continue
                _write_custom_text(channel.id, custom_text)
                mapping["custom_text"] = custom_text
                mappings[cid] = mapping
                mappings_changed = True
                updated.append(channel.name)
                continue

            # On-demand mode
            if custom_text:
                if mapping.get("eas_profile_id"):
                    skipped.append(f"{channel.name} (EAS alert active — text queued, overlay will appear when alert clears)")
                    continue
                if not ticker_pid:
                    # Clone profile and activate
                    try:
                        from apps.channels.models import Channel as _Ch
                        from core.models import StreamProfile as _SP
                        orig = _SP.objects.filter(id=mapping["original_profile_id"]).first()
                        if not orig:
                            skipped.append(f"{channel.name} (original profile not found)")
                            continue
                        style    = mapping.get("custom_style",    "static")
                        position = mapping.get("custom_position", "bottom")
                        schedule = mapping.get("custom_schedule", "always")
                        duration = mapping.get("custom_duration", 10)
                        interval = mapping.get("custom_interval", 5)
                        cloned, _ = _clone_and_inject_custom(
                            channel.id, orig, channel.name, style, position, schedule, duration, interval
                        )
                        ch_obj = _Ch.objects.filter(id=channel.id).first() or channel
                        _assign_profile(ch_obj, cloned)
                        _restart_channel_stream_async(ch_obj, "Custom")
                        mapping["ticker_profile_id"] = cloned.id
                    except Exception as e:
                        skipped.append(f"{channel.name} (clone failed: {e})")
                        logger.warning(f"ticker: on-demand custom clone failed ch{cid}: {e}")
                        continue
                _write_custom_text(channel.id, custom_text)
                mapping["custom_text"] = custom_text
                mappings[cid] = mapping
                mappings_changed = True
                updated.append(channel.name)
            else:
                # Empty text in on-demand → restore passthrough
                if ticker_pid:
                    try:
                        from apps.channels.models import Channel as _Ch
                        ch_obj = _Ch.objects.filter(id=channel.id).first() or channel
                        _restore_profile(ch_obj, mapping["original_profile_id"])
                        _delete_cloned_profile(ticker_pid)
                        _remove_channel_files(channel.id)
                        _restart_channel_stream_async(ch_obj, "Custom")
                    except Exception as e:
                        logger.warning(f"ticker: on-demand custom restore failed ch{cid}: {e}")
                mapping["ticker_profile_id"] = None
                mapping["custom_text"] = ""
                mappings[cid] = mapping
                mappings_changed = True
                cleared.append(channel.name)

        if mappings_changed:
            _save_mappings(mappings)

        parts = []
        if updated: parts.append(f"Updated {len(updated)} channel(s) — text live immediately:\n" + "\n".join(f"  - {u}" for u in updated))
        if cleared: parts.append(f"Cleared {len(cleared)} channel(s) — restored to passthrough:\n" + "\n".join(f"  - {c}" for c in cleared))
        if skipped: parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        return {"success": bool(updated or cleared), "message": "\n\n".join(parts) or "Nothing to do."}

    def _enable_sports(self, params):
        sports_list = [s for s in KNOWN_SPORTS if params.get(f"sports_{s}") in (True, "true", "1", 1)]
        if not sports_list:
            return {"success": False, "message": "Select at least one sport/league before enabling."}

        color_mode   = params.get("sports_color_mode") or "single"
        position     = params.get("sports_position", "bottom")
        fontsize     = int(params.get("sports_fontsize") or 36)
        labelcolor   = params.get("sports_labelcolor") or "#ffd700"
        abbrcolor    = params.get("sports_abbrcolor")  or "#00d4ff"
        favorites    = (params.get("sports_favorites") or "").strip()
        trigger_mode = (params.get("sports_trigger_mode") or "always").strip()

        if trigger_mode == "favorites_only" and not favorites:
            return {"success": False, "message": "Favorite Teams Only mode requires at least one team abbreviation in the Favorite Teams field."}

        channels = self._resolve_channels(params, prefix="sports_")
        if not channels:
            return {"success": False, "message": "No channels found for the selected target."}

        mappings = _get_mappings()
        enabled, skipped, failed = [], [], []

        for channel in channels:
            cid = str(channel.id)
            if cid in mappings:
                existing_type = mappings[cid].get("type", "nowplaying")
                skipped.append(f"{channel.name} (already has a {existing_type} ticker — disable first)")
                continue
            try:
                original_profile = channel.stream_profile
                if not original_profile:
                    try:
                        original_profile = channel.get_stream_profile()
                    except Exception:
                        original_profile = None
                if not original_profile:
                    skipped.append(f"{channel.name} (no stream profile assigned — go to Channels, open this channel, and assign any stream profile, then re-run)")
                    continue
                if not (original_profile.parameters or "").strip():
                    skipped.append(f"{channel.name} (stream profile is Proxy or Redirect — assign an FFmpeg profile to enable this ticker)")
                    continue
                if original_profile.name.startswith(PROFILE_PREFIX):
                    skipped.append(f"{channel.name} (already has a Ticker profile — run Disable Ticker first)")
                    continue

                removed_flags = []
                ticker_profile_id = None

                placeholder = "Loading sports scores..."
                pad = " " * len(placeholder)
                _write_sports_text(channel.id, pad, pad, placeholder, placeholder)

                mappings[cid] = {
                    "original_profile_id":         original_profile.id,
                    "ticker_profile_id":           None,
                    "channel_name":                channel.name,
                    "type":                        "sports",
                    "sports_list":                 sports_list,
                    "sports_favorites":            favorites,
                    "sports_position":             position,
                    "sports_fontsize":             fontsize,
                    "sports_color_mode":           color_mode,
                    "sports_labelcolor":           labelcolor,
                    "sports_abbrcolor":            abbrcolor,
                    "sports_trigger_mode":         trigger_mode,
                    "sports_ticker_style":         "static" if params.get("sports_ticker_static") in (True, "true", "1", 1) else "scrolling",
                    "sports_transcode_mode_video": params.get("sports_transcode_mode_video") or "1080p30",
                    "sports_active":               False,
                    "sports_live":                 False,
                }
                note = (f" [auto-removed: {', '.join(removed_flags)}]" if removed_flags else "")
                enabled.append(f"{channel.name}{note}")

            except Exception as e:
                logger.error(f"ticker: enable_sports failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} (error: {e})")

        _save_mappings(mappings)

        if color_mode == "multi" and _FONT_MONO_BOLD:
            color_note = f"Mode: Multi-Color - sport labels = {labelcolor}, team abbreviations = {abbrcolor}."
        elif color_mode == "multi" and not _FONT_MONO_BOLD:
            color_note = "Mode: Multi-Color requested but monospace font not found — using Single Color white. Install fonts-dejavu or fonts-liberation to enable colors."
        else:
            color_note = "Mode: Single Color white (1 drawtext layer — lower CPU)."
        parts = []
        sports_label = ", ".join(LABELS.get(s, s.upper()) for s in sports_list)
        trigger_note = {
            "always":         "Trigger: Always — ticker activates on-demand when a viewer connects (all scores shown, including finals).",
            "live_only":      "Trigger: Active Games Only — ticker activates on-demand when a viewer connects and a live game is in progress.",
            "favorites_only": f"Trigger: Favorite Teams Only — ticker activates on-demand when a viewer connects and a team from [{favorites}] is playing.",
        }.get(trigger_mode, "")
        if enabled:
            parts.append(
                f"Enabled sports ticker on {len(enabled)} channel(s)\n"
                f"Sports: {sports_label}\n"
                f"{trigger_note}\n"
                f"{color_note}\n"
                + "\n".join(f"  - {n}" for n in enabled)
            )
        if skipped:
            parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:
            parts.append("Failed:\n" + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _update_sports(self, params):
        """Update sports settings on already-enabled channels without restarting the stream.
        Updates sports_list and display settings in mappings.json; the sweep loop picks up the
        new sports_list on its next cycle and reload=1 delivers it without a stream restart."""
        sports_list = [s for s in KNOWN_SPORTS if params.get(f"sports_{s}") in (True, "true", "1", 1)]
        if not sports_list:
            return {"success": False, "message": "Select at least one sport/league."}

        color_mode   = params.get("sports_color_mode") or "single"
        position     = params.get("sports_position", "bottom")
        fontsize     = int(params.get("sports_fontsize") or 36)
        labelcolor   = params.get("sports_labelcolor") or "#ffd700"
        abbrcolor    = params.get("sports_abbrcolor")  or "#00d4ff"
        favorites    = (params.get("sports_favorites") or "").strip()
        trigger_mode = (params.get("sports_trigger_mode") or "always").strip()
        transcode_mode = (params.get("sports_transcode_mode_video") or "").strip() or None

        if trigger_mode == "favorites_only" and not favorites:
            return {"success": False, "message": "Favorite Teams Only mode requires at least one team abbreviation in the Favorite Teams field."}

        channels = self._resolve_channels(params, prefix="sports_")
        if not channels:
            return {"success": False, "message": "No channels found for the selected target."}

        mappings = _get_mappings()
        updated, skipped, failed = [], [], []

        for channel in channels:
            cid = str(channel.id)
            mapping = mappings.get(cid)
            if not mapping or mapping.get("type") != "sports":
                skipped.append(f"{channel.name} (no sports ticker — enable it first)")
                continue
            try:
                mapping["sports_list"]                 = sports_list
                mapping["sports_favorites"]            = favorites
                mapping["sports_position"]             = position
                mapping["sports_fontsize"]             = fontsize
                mapping["sports_color_mode"]           = color_mode
                mapping["sports_labelcolor"]           = labelcolor
                mapping["sports_abbrcolor"]            = abbrcolor
                mapping["sports_trigger_mode"]         = trigger_mode
                mapping["sports_ticker_style"]         = "static" if params.get("sports_ticker_static") in (True, "true", "1", 1) else "scrolling"
                mapping["sports_transcode_mode_video"] = transcode_mode or mapping.get("sports_transcode_mode_video") or "1080p30"
                mappings[cid] = mapping
                updated.append(channel.name)
            except Exception as e:
                logger.error(f"ticker: update_sports failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} (error: {e})")

        _save_mappings(mappings)

        sports_label = ", ".join(LABELS.get(s, s.upper()) for s in sports_list)
        parts = []
        if updated:
            parts.append(
                f"Updated sports ticker on {len(updated)} channel(s) — ticker content will refresh on the next sweep cycle (within ~30s):\n"
                f"Sports: {sports_label}\n"
                + "\n".join(f"  - {n}" for n in updated)
            )
        if skipped:
            parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:
            parts.append("Failed:\n" + "\n".join(f"  - {f}" for f in failed))
        return {"success": bool(updated), "message": "\n\n".join(parts) or "Nothing to do."}

    # ------------------------------------------------------------------ #
    # Shared disable helper + phase-specific disable actions             #
    # ------------------------------------------------------------------ #

    def _do_disable(self, channels, type_filter=None):
        """Core disable logic shared by all three phase-specific disable actions."""
        mappings = _get_mappings()
        disabled, skipped, failed = [], [], []

        for channel in channels:
            cid = str(channel.id)
            mapping = mappings.get(cid)
            if not mapping:
                skipped.append(f"{channel.name} (no ticker active on this channel)")
                continue
            ticker_type = mapping.get("type", "nowplaying")
            if type_filter and ticker_type != type_filter:
                skipped.append(f"{channel.name} (has a {ticker_type} ticker, not {type_filter} — use the correct disable action)")
                continue
            try:
                _restore_profile(channel, mapping["original_profile_id"])
                _delete_cloned_profile(mapping["ticker_profile_id"])
                _remove_channel_files(channel.id)
                if ticker_type == "custom":
                    _remove_custom_file(channel.id)
                elif ticker_type == "sports":
                    _remove_sports_file(channel.id)
                if mapping.get("eas_armed"):
                    # EAS is co-armed — preserve it as a pure EAS channel rather than deleting the mapping
                    mappings[cid] = {
                        "original_profile_id": mapping["original_profile_id"],
                        "channel_name":        channel.name,
                        "type":                "eas",
                    }
                else:
                    del mappings[cid]
                disabled.append(channel.name)
            except Exception as e:
                logger.error(f"ticker: disable failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} (error: {e})")

        _save_mappings(mappings)
        parts = []
        if disabled: parts.append(f"Disabled: {len(disabled)} channel(s)")
        if skipped:  parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:   parts.append("Failed:\n"  + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _disable_nowplaying(self, params):
        channels = self._resolve_channels(params, prefix="np_")
        if not channels:
            return {"success": False, "message": "No channels found. Set the Now Playing > Apply To / Channel selector above."}
        return self._do_disable(channels, type_filter="nowplaying")

    def _disable_custom_ticker(self, params):
        channels = self._resolve_channels(params, prefix="custom_")
        if not channels:
            return {"success": False, "message": "No channels found. Set the Custom Text > Apply To / Channel selector above."}
        return self._do_disable(channels, type_filter="custom")

    def _disable_sports_ticker(self, params):
        channels = self._resolve_channels(params, prefix="sports_")
        if not channels:
            return {"success": False, "message": "No channels found. Set the Sports Ticker > Apply To / Channel selector above."}
        return self._do_disable(channels, type_filter="sports")

    def _enable_eas(self, params):
        zones = (params.get("eas_zones") or "").strip()
        if not zones:
            return {"success": False, "message": "No NWS zone codes configured. Enter at least one zone code in EAS Weather Alerts > NWS Zone / County Codes above."}

        channels = self._resolve_channels(params, prefix="eas_")
        if not channels:
            return {"success": False, "message": "No channels found. Set the EAS Weather Alerts > Apply To / Channel selector above."}

        mappings = _get_mappings()
        enabled, skipped, failed = [], [], []

        for channel in channels:
            cid = str(channel.id)
            try:
                if cid in mappings:
                    existing = mappings[cid]
                    if existing.get("type") == "eas" or existing.get("eas_armed"):
                        skipped.append(f"{channel.name} (EAS already armed — already enabled)")
                        continue
                    if existing.get("eas_profile_id"):
                        skipped.append(f"{channel.name} (EAS alert currently active — wait for it to clear)")
                        continue
                    # Co-arm EAS alongside the existing ticker without disrupting it
                    _eas_clear(channel.id)
                    existing["eas_armed"] = True
                    mappings[cid] = existing
                    enabled.append(channel.name)
                    continue
                original_profile = channel.stream_profile
                if not original_profile:
                    try:
                        original_profile = channel.get_stream_profile()
                    except Exception:
                        original_profile = None
                if not original_profile:
                    skipped.append(f"{channel.name} (no stream profile assigned — assign one in Channels first)")
                    continue
                if not (original_profile.parameters or "").strip():
                    skipped.append(f"{channel.name} (stream profile is Proxy or Redirect — assign an FFmpeg profile to enable this ticker)")
                    continue
                if original_profile.name.startswith(PROFILE_PREFIX):
                    skipped.append(f"{channel.name} (already has a Ticker profile — disable first)")
                    continue
                _eas_clear(channel.id)
                mappings[cid] = {
                    "original_profile_id": original_profile.id,
                    "channel_name":        channel.name,
                    "type":                "eas",
                }
                enabled.append(channel.name)
            except Exception as e:
                logger.error(f"ticker: enable EAS failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} ({e})")

        _save_mappings(mappings)
        parts = []
        if enabled:
            parts.append(f"EAS registered: {len(enabled)} channel(s)\n" + "\n".join(f"  - {e}" for e in enabled))
            parts.append(f"Monitoring zones: {zones}\nChannels continue using their normal profile. When a qualifying NWS alert fires, they automatically switch to the EAS overlay and restart. No re-encoding overhead until an actual alert occurs.")
        if skipped: parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:  parts.append("Failed:\n"  + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _disable_eas(self, params):
        channels = self._resolve_channels(params, prefix="eas_")
        if not channels:
            return {"success": False, "message": "No channels found. Set the EAS Weather Alerts > Apply To / Channel selector above."}
        mappings = _get_mappings()
        disabled, skipped, failed = [], [], []
        for channel in channels:
            cid = str(channel.id)
            mapping = mappings.get(cid)
            is_pure_eas = mapping and mapping.get("type") == "eas"
            is_coarmed  = mapping and mapping.get("eas_armed")
            if not mapping or (not is_pure_eas and not is_coarmed):
                skipped.append(f"{channel.name} (no EAS ticker active)")
                continue
            try:
                if mapping.get("eas_profile_id"):
                    _restore_profile(channel, mapping["original_profile_id"])
                    _delete_cloned_profile(mapping["eas_profile_id"])
                _eas_clear(channel.id)
                with _eas_lock:
                    _eas_active.pop(cid, None)
                if is_pure_eas:
                    del mappings[cid]
                else:
                    # Co-armed: remove EAS keys, leave the ticker mapping intact
                    mapping.pop("eas_armed", None)
                    mapping.pop("eas_profile_id", None)
                    mappings[cid] = mapping
                disabled.append(channel.name)
            except Exception as e:
                logger.error(f"ticker: disable EAS failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} ({e})")
        _save_mappings(mappings)
        parts = []
        if disabled: parts.append(f"EAS disabled: {len(disabled)} channel(s)")
        if skipped:  parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:   parts.append("Failed:\n"  + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _lookup_eas_zones(self, params):
        state = (params.get("eas_zone_lookup_state") or "").strip().upper()
        if not state:
            return {"success": False, "message": "Enter a 2-letter US state code in the Zone Lookup field above (e.g. TX, WA, FL)."}
        if len(state) != 2 or not state.isalpha():
            return {"success": False, "message": f"'{state}' is not a valid state code. Use the 2-letter abbreviation (e.g. TX, WA, FL, NY)."}
        url = f"https://api.weather.gov/zones?type=public&area={state}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": NWS_UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except Exception as e:
            return {"success": False, "message": f"NWS API error: {e}"}
        features = data.get("features") or []
        if not features:
            return {"success": False, "message": f"No public zones found for state: {state}. Check the state code and try again."}
        features.sort(key=lambda x: (x.get("properties") or {}).get("id") or "")
        lines = [f"NWS public zones for {state} — {len(features)} zones:"]
        for f in features:
            p = f.get("properties") or {}
            lines.append(f"  {p.get('id','?')} — {p.get('name','')}")
        lines.append(f"\nPaste the code(s) you want into NWS Alert Zones above, comma-separated (e.g. TXZ001,TXZ002).")
        return {"success": True, "message": "\n".join(lines)}

    def _lookup_eas_ca(self, params):
        query = (params.get("eas_ca_lookup_query") or "").strip()
        if not query:
            return {"success": False, "message": "Enter a city name or province code in the City Lookup field above (e.g. Toronto, ON, Montréal)."}

        if not os.path.exists(_EAS_CA_CITY_NAMES):
            return {"success": False, "message": "City names reference file not found. Please update Ticker to get the bundled city list."}
        try:
            with open(_EAS_CA_CITY_NAMES, encoding="utf-8") as f:
                city_names = json.load(f)
        except Exception as e:
            return {"success": False, "message": f"Could not load city names file: {e}"}

        q = query.lower().strip()

        # Province code lookup: if query is 2 letters, treat as province prefix
        if len(q) == 2 and q.isalpha():
            matches = [
                (cid, names) for cid, names in city_names.items()
                if cid.startswith(f"{q}-")
            ]
            label = f"province '{query.upper()}'"
        else:
            # City name search — check both English and French names
            matches = [
                (cid, names) for cid, names in city_names.items()
                if q in (names.get("en") or "").lower() or q in (names.get("fr") or "").lower()
            ]
            label = f"'{query}'"

        if not matches:
            return {"success": False, "message": f"No cities found matching {label}. Try a shorter search or use a 2-letter province code (e.g. ON, QC, BC, AB)."}

        matches.sort(key=lambda x: x[0])
        lines = [f"Weather Canada cities matching {label} — {len(matches)} result(s):"]
        for cid, names in matches:
            en = names.get("en") or ""
            fr = names.get("fr") or ""
            if en == fr or not fr:
                lines.append(f"  {cid} — {en}")
            else:
                lines.append(f"  {cid} — {en} / {fr}")
        lines.append(f"\nPaste the code(s) you want into Weather Canada City IDs above, comma-separated.")
        return {"success": True, "message": "\n".join(lines)}

    def _enable_eas_ca(self, params):
        ca_ids = (params.get("eas_ca_city_ids") or "").strip()
        if not ca_ids:
            return {"success": False, "message": "No Weather Canada city IDs configured. Enter at least one city ID in EAS Weather Alerts > Weather Canada City IDs above."}

        channels = self._resolve_channels(params, prefix="eas_ca_")
        if not channels:
            return {"success": False, "message": "No channels found. Set the Weather Canada — Apply To / Channel selector in Settings."}

        mappings = _get_mappings()
        enabled, skipped, failed = [], [], []

        for channel in channels:
            cid = str(channel.id)
            try:
                if cid in mappings:
                    existing = mappings[cid]
                    if existing.get("type") == "eas_ca" or existing.get("eas_ca_armed"):
                        skipped.append(f"{channel.name} (Weather Canada EAS already armed)")
                        continue
                    if existing.get("eas_profile_id"):
                        skipped.append(f"{channel.name} (EAS alert currently active — wait for it to clear)")
                        continue
                    existing["eas_ca_armed"] = True
                    mappings[cid] = existing
                    enabled.append(channel.name)
                    continue
                original_profile = channel.stream_profile
                if not original_profile:
                    try:
                        original_profile = channel.get_stream_profile()
                    except Exception:
                        original_profile = None
                if not original_profile:
                    skipped.append(f"{channel.name} (no stream profile assigned — assign one in Channels first)")
                    continue
                if not (original_profile.parameters or "").strip():
                    skipped.append(f"{channel.name} (stream profile is Proxy or Redirect — assign an FFmpeg profile to enable this ticker)")
                    continue
                if original_profile.name.startswith(PROFILE_PREFIX):
                    skipped.append(f"{channel.name} (already has a Ticker profile — disable first)")
                    continue
                mappings[cid] = {
                    "original_profile_id": original_profile.id,
                    "channel_name":        channel.name,
                    "type":                "eas_ca",
                }
                enabled.append(channel.name)
            except Exception as e:
                logger.error(f"ticker: enable EAS CA failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} ({e})")

        _save_mappings(mappings)
        parts = []
        if enabled:
            parts.append(f"Weather Canada EAS registered: {len(enabled)} channel(s)\n" + "\n".join(f"  - {e}" for e in enabled))
            parts.append(f"Monitoring city IDs: {ca_ids}\nChannels continue using their normal profile. When a qualifying Environment Canada alert fires, they automatically switch to the EAS overlay. No re-encoding overhead until an actual alert occurs.")
        if skipped: parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:  parts.append("Failed:\n"  + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _disable_eas_ca(self, params):
        channels = self._resolve_channels(params, prefix="eas_ca_")
        if not channels:
            return {"success": False, "message": "No channels found. Set the Weather Canada — Apply To / Channel selector in Settings."}
        mappings = _get_mappings()
        disabled, skipped, failed = [], [], []
        for channel in channels:
            cid = str(channel.id)
            mapping = mappings.get(cid)
            is_pure_ca = mapping and mapping.get("type") == "eas_ca"
            is_coarmed = mapping and mapping.get("eas_ca_armed")
            if not mapping or (not is_pure_ca and not is_coarmed):
                skipped.append(f"{channel.name} (no Weather Canada EAS active)")
                continue
            try:
                if mapping.get("eas_profile_id"):
                    _restore_profile(channel, mapping["original_profile_id"])
                    _delete_cloned_profile(mapping["eas_profile_id"])
                _eas_clear(channel.id)
                with _eas_lock:
                    _eas_active.pop(cid, None)
                if is_pure_ca:
                    del mappings[cid]
                else:
                    mapping.pop("eas_ca_armed", None)
                    mapping.pop("eas_profile_id", None)
                    mappings[cid] = mapping
                disabled.append(channel.name)
            except Exception as e:
                logger.error(f"ticker: disable EAS CA failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} ({e})")
        _save_mappings(mappings)
        parts = []
        if disabled: parts.append(f"Weather Canada EAS disabled: {len(disabled)} channel(s)")
        if skipped:  parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:   parts.append("Failed:\n"  + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _test_eas_ca(self, params):
        """Fire a fake Weather Canada alert on CA EAS-enabled channels for a configurable duration, then auto-restore."""
        import threading as _threading
        duration = 60
        try:
            duration = max(10, min(600, int(params.get("eas_test_duration") or 60)))
        except (ValueError, TypeError):
            pass

        channels = self._resolve_channels(params, prefix="eas_ca_")
        if not channels:
            return {"success": False, "message": "No channels found. Set the Weather Canada — Apply To / Channel selector in Settings."}

        mappings = _get_mappings()
        settings = _get_settings()
        fired, skipped, failed = [], [], []

        for channel in channels:
            cid = str(channel.id)
            mapping = mappings.get(cid)
            if not mapping or (mapping.get("type") != "eas_ca" and not mapping.get("eas_ca_armed")):
                skipped.append(f"{channel.name} (no Weather Canada EAS active — enable it first)")
                continue
            if mapping.get("eas_profile_id"):
                skipped.append(f"{channel.name} (EAS already active — clear it first)")
                continue
            try:
                from core.models import StreamProfile
                orig = StreamProfile.objects.filter(id=mapping["original_profile_id"]).first()
                if not orig:
                    failed.append(f"{channel.name} (original profile missing)")
                    continue

                tone_interval  = int(settings.get("eas_tone_interval") or 300)
                tone_interval  = max(30, tone_interval) if tone_interval > 0 else 0
                overlay_style  = settings.get("eas_overlay_style")  or "ticker"
                label_color    = settings.get("eas_label_color")     or "0xCC0000"
                transcode_mode = settings.get("eas_transcode_mode")  or "full"

                tone_wav = None
                if tone_interval > 0 and os.path.exists(_EAS_CA_TONE_FILE):
                    tone_wav = _EAS_CA_TONE_FILE

                eas_profile, _ = _clone_and_inject_eas(
                    channel.id, orig, channel.name,
                    tone_interval, overlay_style, label_color, transcode_mode,
                    tone_wav=tone_wav,
                )
                _assign_profile(channel, eas_profile)

                fake_alerts = [{
                    "event":    "TEST — Weather Canada EAS Test Alert",
                    "area":     "Test Zone — ceci n'est qu'un test / this is only a test",
                    "severity": "Moderate",
                    "source":   "EC",
                }]
                _eas_write_alert(cid, fake_alerts, overlay_style)
                mapping["eas_profile_id"] = eas_profile.id
                mappings[cid] = mapping
                _eas_restart_channel(str(channel.uuid))
                fired.append((cid, channel.name, eas_profile.id, str(channel.uuid)))

            except Exception as e:
                logger.error(f"ticker: test_eas_ca failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} (error: {e})")

        if fired:
            _save_mappings(mappings)

            def _auto_restore_ca(entries, delay):
                import time as _t
                _t.sleep(delay)
                try:
                    m = _get_mappings()
                    from apps.channels.models import Channel as _Ch
                    from core.models import StreamProfile as _SP
                    for _cid, _name, _eid, _uuid in entries:
                        _mapping = m.get(_cid)
                        if not _mapping:
                            continue
                        try:
                            _ch = _Ch.objects.filter(id=int(_cid)).first()
                            if _ch:
                                _restore_profile(_ch, _mapping.get("original_profile_id"))
                            if _eid:
                                _delete_cloned_profile(_eid)
                            _mapping.pop("eas_profile_id", None)
                            m[_cid] = _mapping
                            _eas_clear(_cid)
                            with _eas_lock:
                                _eas_active.pop(_cid, None)
                            if _ch:
                                _eas_restart_channel(_uuid)
                            logger.info(f"ticker: test EAS CA auto-restored ch {_cid}")
                        except Exception as _e:
                            logger.error(f"ticker: test EAS CA restore failed ch {_cid}: {_e}")
                    _save_mappings(m)
                except Exception as _e:
                    logger.error(f"ticker: test EAS CA auto-restore thread failed: {_e}")

            _threading.Thread(target=_auto_restore_ca, args=(list(fired), duration), daemon=True).start()

        parts = []
        if fired:
            names = [n for _, n, _, _ in fired]
            parts.append(f"Weather Canada test alert fired on {len(fired)} channel(s): {', '.join(names)}\nOverlay will auto-restore in {duration} seconds.")
        if skipped: parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:  parts.append("Failed:\n"  + "\n".join(f"  - {f}" for f in failed))
        return {"success": bool(fired) and not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _disable_all(self, params):
        from apps.channels.models import Channel

        mappings = _get_mappings()
        if not mappings:
            return {"success": True, "message": "No active tickers to disable."}

        disabled, failed = [], []
        for cid, mapping in list(mappings.items()):
            name = mapping.get("channel_name", f"Channel {cid}")
            try:
                channel = Channel.objects.get(id=int(cid))
                _restore_profile(channel, mapping["original_profile_id"])
                for pid_key in ("ticker_profile_id", "eas_profile_id"):
                    if mapping.get(pid_key):
                        _delete_cloned_profile(mapping[pid_key])
                ticker_type = mapping.get("type", "nowplaying")
                _remove_channel_files(channel.id)
                if ticker_type == "custom":
                    _remove_custom_file(channel.id)
                elif ticker_type == "sports":
                    _remove_sports_file(channel.id)
                elif ticker_type in ("eas", "eas_ca"):
                    _eas_clear(channel.id)
                    with _eas_lock:
                        _eas_active.pop(cid, None)
                if mapping.get("eas_armed") or mapping.get("eas_ca_armed"):
                    _eas_clear(channel.id)
                    with _eas_lock:
                        _eas_active.pop(cid, None)
                del mappings[cid]
                disabled.append(name)
            except Exception as e:
                logger.error(f"ticker: disable_all failed for {name}: {e}", exc_info=True)
                failed.append(f"{name} (error: {e})")

        _save_mappings(mappings)
        parts = []
        if disabled: parts.append(f"Disabled: {len(disabled)} channel(s)\n" + "\n".join(f"  - {n}" for n in disabled))
        if failed:   parts.append("Failed:\n" + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _test_eas(self, params):
        """Fire a fake EAS alert on EAS-enabled channels for a configurable duration, then auto-restore."""
        import threading as _threading
        duration = 60
        try:
            duration = max(10, min(600, int(params.get("eas_test_duration") or 60)))
        except (ValueError, TypeError):
            pass

        channels = self._resolve_channels(params, prefix="eas_")
        if not channels:
            return {"success": False, "message": "No channels found. Select a channel in the EAS section."}

        mappings  = _get_mappings()
        settings  = _get_settings()
        fired, skipped, failed = [], [], []

        for channel in channels:
            cid = str(channel.id)
            mapping = mappings.get(cid)
            if not mapping or (mapping.get("type") != "eas" and not mapping.get("eas_armed")):
                skipped.append(f"{channel.name} (no EAS ticker active — enable it first)")
                continue
            if mapping.get("eas_profile_id"):
                skipped.append(f"{channel.name} (EAS already active — clear it first)")
                continue
            try:
                from core.models import StreamProfile
                orig = StreamProfile.objects.filter(id=mapping["original_profile_id"]).first()
                if not orig:
                    failed.append(f"{channel.name} (original profile missing)")
                    continue

                tone_interval  = int(settings.get("eas_tone_interval") or 300)
                tone_interval  = max(30, tone_interval) if tone_interval > 0 else 0
                overlay_style  = settings.get("eas_overlay_style")  or "ticker"
                label_color    = settings.get("eas_label_color")     or "0xCC0000"
                transcode_mode = settings.get("eas_transcode_mode")  or "full"

                eas_profile, _ = _clone_and_inject_eas(
                    channel.id, orig, channel.name,
                    tone_interval, overlay_style, label_color, transcode_mode,
                )
                _assign_profile(channel, eas_profile)

                fake_alerts = [{
                    "event":    "TEST — Ticker EAS Test Alert",
                    "area":     "Test Zone — this is only a test",
                    "severity": "Moderate",
                    "headline": f"TEST: EAS overlay firing for {duration} seconds. No action required.",
                }]
                _eas_write_alert(cid, fake_alerts, overlay_style)
                mapping["eas_profile_id"] = eas_profile.id
                mappings[cid] = mapping
                _eas_restart_channel(str(channel.uuid))
                fired.append((cid, channel.name, eas_profile.id, str(channel.uuid)))

            except Exception as e:
                logger.error(f"ticker: test_eas failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} (error: {e})")

        if fired:
            _save_mappings(mappings)

            def _auto_restore(entries, delay):
                import time as _t
                _t.sleep(delay)
                try:
                    m = _get_mappings()
                    from apps.channels.models import Channel as _Ch
                    for cid, ch_name, eas_pid, ch_uuid in entries:
                        mapping = m.get(cid)
                        if not mapping:
                            continue
                        ch = _Ch.objects.filter(id=int(cid)).first()
                        if ch:
                            _restore_profile(ch, mapping["original_profile_id"])
                            _eas_restart_channel(ch_uuid)
                        _delete_cloned_profile(eas_pid)
                        mapping.pop("eas_profile_id", None)
                        m[cid] = mapping
                    _save_mappings(m)
                    _eas_clear(int(entries[0][0]))
                    logger.info(f"ticker: EAS test auto-restored after {delay}s")
                except Exception as e:
                    logger.error(f"ticker: EAS test auto-restore failed: {e}", exc_info=True)

            t = _threading.Thread(target=_auto_restore, args=(fired, duration), daemon=True)
            t.start()

        parts = []
        if fired:
            names = ", ".join(n for _, n, _, _ in fired)
            parts.append(f"EAS test alert fired on: {names}\nWill auto-restore in {duration} seconds.")
        if skipped:
            parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:
            parts.append("Failed:\n" + "\n".join(f"  - {f}" for f in failed))
        return {"success": bool(fired), "message": "\n\n".join(parts) or "Nothing to do."}

    def _test_sports(self, params):
        """Fire fake sports ticker content on sports-enabled channels for a configurable duration, then auto-restore."""
        import threading as _threading
        duration = 60
        try:
            duration = max(10, min(600, int(params.get("sports_test_duration") or 60)))
        except (ValueError, TypeError):
            pass

        channels = self._resolve_channels(params, prefix="sports_")
        if not channels:
            return {"success": False, "message": "No channels found. Select a channel in the Sports Ticker section."}

        mappings = _get_mappings()
        fired, skipped, failed = [], [], []

        for channel in channels:
            cid = str(channel.id)
            mapping = mappings.get(cid)
            if not mapping or mapping.get("type") != "sports":
                skipped.append(f"{channel.name} (no sports ticker active — enable it first)")
                continue
            if mapping.get("sports_active"):
                skipped.append(f"{channel.name} (sports ticker already active — wait for games to end or disable first)")
                continue
            try:
                from core.models import StreamProfile
                orig = StreamProfile.objects.filter(id=mapping["original_profile_id"]).first()
                if not orig:
                    failed.append(f"{channel.name} (original profile missing)")
                    continue

                # Write fake ticker content
                cid_int = int(cid)
                fake_full = "  [TEST] HOU 4 @ NYY 2 (Bot 7th)    [TEST] LAD 1 @ CHC 6 (Final)    [TEST] KC 3 @ BOS 5 (Final)    Sports Ticker Test — this is only a test  "
                fake_pad  = " " * len(fake_full)
                # Use fake_full for all layers so single-color renders correctly.
                # Multi-color layers align by column — fake data can't replicate that,
                # so we force single-color in the test to avoid triple-stacked rendering
                # that makes the font appear larger than it really is.
                _write_sports_text(cid_int, fake_pad, fake_full, fake_full, fake_full)

                # Build overlay using the channel's current settings
                position       = mapping.get("sports_position", "bottom")
                fontsize       = int(mapping.get("sports_fontsize") or 36)
                color_mode     = "single"  # force single-color for test — multi-color needs columnar data
                labelcolor     = mapping.get("sports_labelcolor", "#ffd700")
                abbrcolor      = mapping.get("sports_abbrcolor", "#00d4ff")
                transcode_mode = mapping.get("sports_transcode_mode_video", "1080p30")
                ticker_style   = mapping.get("sports_ticker_style", "scrolling")

                if _supports_channel_id_token():
                    # Get-or-create by config signature already dedupes repeated test runs —
                    # no need for (and no safety in) mutating whatever profile ticker_pid
                    # happens to point at, which under shared profiles could be in active use
                    # by other channels.
                    sports_profile, _ = _clone_and_inject_sports(
                        cid_int, orig, channel.name, position, fontsize,
                        labelcolor, abbrcolor, color_mode, transcode_mode, ticker_style
                    )
                    mapping["ticker_profile_id"] = sports_profile.id
                else:
                    raw_params, _ = _strip_dangerous_flags(channel.name, orig.parameters or "")
                    drawtext = _build_sports_filter(cid_int, position, fontsize,
                                                    labelcolor, abbrcolor, color_mode,
                                                    transcode_mode, ticker_style)
                    new_params = _inject_drawtext(raw_params, drawtext)

                    ticker_pid = mapping.get("ticker_profile_id")
                    existing = StreamProfile.objects.filter(id=ticker_pid).first() if ticker_pid else None
                    if existing and existing.name.startswith(PROFILE_PREFIX):
                        existing.parameters = new_params
                        existing.save()
                        sports_profile = existing
                    else:
                        sports_profile, _ = _clone_and_inject(cid_int, orig, channel.name)
                        sports_profile.parameters = new_params
                        sports_profile.save()
                        mapping["ticker_profile_id"] = sports_profile.id

                _assign_profile(channel, sports_profile)
                _restart_channel_stream_async(channel, "Sports-Test")

                mapping["sports_active"] = True
                mapping.pop("no_viewer_since", None)
                mappings[cid] = mapping
                fired.append((cid, channel.name, mapping["original_profile_id"], mapping["ticker_profile_id"]))

            except Exception as e:
                logger.error(f"ticker: test_sports failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} (error: {e})")

        if fired:
            _save_mappings(mappings)

            def _auto_restore(entries, delay):
                import time as _t
                _t.sleep(delay)
                try:
                    m = _get_mappings()
                    from apps.channels.models import Channel as _Ch
                    for cid, ch_name, orig_pid, ticker_pid in entries:
                        mapping = m.get(cid)
                        if not mapping:
                            continue
                        ch = _Ch.objects.filter(id=int(cid)).first()
                        if ch:
                            _restore_profile(ch, orig_pid)
                            _restart_channel_stream_async(ch, "Sports-Test-Restore")
                        mapping["sports_active"] = False
                        m[cid] = mapping
                    _save_mappings(m)
                    logger.info(f"ticker: sports test auto-restored after {delay}s")
                except Exception as e:
                    logger.error(f"ticker: sports test auto-restore failed: {e}", exc_info=True)

            t = _threading.Thread(target=_auto_restore, args=(fired, duration), daemon=True)
            t.start()

        parts = []
        if fired:
            names = ", ".join(n for _, n, _, _ in fired)
            parts.append(
                f"Sports ticker test fired on: {names}\n"
                f"Will auto-restore in {duration} seconds.\n\n"
                f"Tune to the channel now to see your ticker settings in action. "
                f"The test uses fake scores — not real game data."
            )
        if skipped:
            parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:
            parts.append("Failed:\n" + "\n".join(f"  - {f}" for f in failed))
        return {"success": bool(fired), "message": "\n\n".join(parts) or "Nothing to do."}

    def _migrate_eas(self, params):
        """Migrate old always-on EAS profiles (ticker_profile_id) to dynamic mode."""
        from apps.channels.models import Channel

        mappings = _get_mappings()
        old_eas = {cid: m for cid, m in mappings.items()
                   if m and m.get("type") == "eas" and m.get("ticker_profile_id")}
        if not old_eas:
            return {"success": True, "message": "No old static EAS profiles found - all EAS channels are already in dynamic mode."}

        migrated, failed = [], []
        for cid, mapping in old_eas.items():
            name = mapping.get("channel_name", f"Channel {cid}")
            try:
                channel = Channel.objects.filter(id=int(cid)).first()
                if channel:
                    _restore_profile(channel, mapping["original_profile_id"])
                    _eas_restart_channel(str(channel.uuid))
                _delete_cloned_profile(mapping["ticker_profile_id"])
                mapping.pop("ticker_profile_id", None)
                mapping.pop("eas_profile_id", None)
                mappings[cid] = mapping
                with _eas_lock:
                    _eas_active.pop(cid, None)
                migrated.append(name)
            except Exception as e:
                logger.error(f"ticker: migrate EAS failed for {name}: {e}", exc_info=True)
                failed.append(f"{name} ({e})")

        _save_mappings(mappings)
        rc = _get_redis_client()
        if rc:
            try:
                rc.delete(_EAS_STATE_KEY, _EAS_OWNER_KEY)
            except Exception:
                pass

        parts = []
        if migrated:
            parts.append(f"Migrated {len(migrated)} channel(s) to dynamic EAS:\n" + "\n".join(f"  - {n}" for n in migrated))
            parts.append("Channels restored to passthrough profiles. Re-encoding only occurs when a real NWS alert fires.")
        if failed:
            parts.append("Failed:\n" + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _save_settings(self, params):
        """No-op action whose only purpose is to give Dispatcharr's plugin UI a
        reason to persist the current Settings form -- Dispatcharr only POSTs the
        settings form alongside an action run, not on field blur/change, so a
        setting toggled and left alone (dialog closed without running anything
        else) silently reverts to its old saved value next session. Click this
        after changing Settings to be sure the change actually stuck."""
        return {"success": True, "message": "Settings saved."}

    def _view_active(self, params):
        mappings = _get_mappings()
        if not mappings:
            return {"success": True, "message": "No channels currently enabled."}

        np_channels     = [(cid, m) for cid, m in mappings.items() if m.get("type", "nowplaying") == "nowplaying"]
        custom_channels = [(cid, m) for cid, m in mappings.items() if m.get("type") == "custom"]
        sports_channels = [(cid, m) for cid, m in mappings.items() if m.get("type") == "sports"]

        lines = [
            f"Total: {len(mappings)}  |  Now Playing: {len(np_channels)}  "
            f"|  Custom: {len(custom_channels)}  |  Sports: {len(sports_channels)}",
            "",
        ]

        if np_channels:
            with_deeplink = [(cid, m) for cid, m in np_channels if m.get("xm_deeplink")]
            no_deeplink   = [(cid, m) for cid, m in np_channels if not m.get("xm_deeplink")]
            lines.append(f"-- Now Playing ({len(np_channels)}, {len(with_deeplink)} matched to ticker.com) --")
            for cid, mapping in with_deeplink[:25]:
                name = mapping.get("channel_name", f"Channel {cid}")
                deeplink = mapping.get("xm_deeplink")
                artist_file = os.path.join(TICKER_DIR, f"channel_{cid}_artist.txt")
                song_file   = os.path.join(TICKER_DIR, f"channel_{cid}_song.txt")
                try:
                    with open(artist_file, encoding="utf-8") as f: artist = f.read().strip()
                    with open(song_file,   encoding="utf-8") as f: song   = f.read().strip()
                except Exception:
                    artist = song = ""
                if artist or song:
                    lines.append(f"  [{deeplink}] {name}: {artist} - {song}")
                else:
                    lines.append(f"  [{deeplink}] {name}: (no data yet)")
            if no_deeplink:
                lines.append(f"  No ticker.com match ({len(no_deeplink)} - run Refresh Channel Data):")
                for cid, m in no_deeplink[:5]:
                    lines.append(f"    - {m.get('channel_name', cid)}")
            lines.append("")

        if custom_channels:
            lines.append(f"-- Custom Text ({len(custom_channels)}) --")
            for cid, mapping in custom_channels[:20]:
                name  = mapping.get("channel_name", f"Channel {cid}")
                style = mapping.get("custom_style", "static")
                text  = mapping.get("custom_text", "")[:60]
                lines.append(f"  {name}: [{style}] {text}")
            lines.append("")

        if sports_channels:
            lines.append(f"-- Sports Ticker ({len(sports_channels)}) --")
            for cid, mapping in sports_channels[:20]:
                name = mapping.get("channel_name", f"Channel {cid}")
                sl   = mapping.get("sports_list", [])
                fav  = mapping.get("sports_favorites", "")
                labels_str = ", ".join(LABELS.get(s, s) for s in sl)
                sports_file = os.path.join(TICKER_DIR, f"channel_{cid}_sports_full.txt")
                try:
                    with open(sports_file, encoding="utf-8") as f:
                        preview = f.read().strip()[:100]
                except FileNotFoundError:
                    preview = "(no data yet)"
                except Exception:
                    preview = "(error)"
                lines.append(f"  {name}: [{labels_str}]" + (f"  favs: {fav}" if fav else ""))
                lines.append(f"    {preview}")

        return {"success": True, "message": "\n".join(lines)}

    def _refresh_channels(self, params):
        try:
            channels, aliases = _get_channel_data(force=True)
            stations = _get_stations(force=True)
            if not stations:
                return {"success": False, "message": "ticker.com channel list came back empty - try again in a moment."}
            mappings = _get_mappings()
            deeplinks_fixed = 0
            descs_fixed = 0
            unmatched = []
            for cid, mapping in mappings.items():
                channel_name = mapping.get("channel_name", "")
                xm_entry = _match_channel(channel_name, channels, aliases)

                # Always update description if we have one and it's currently empty
                if xm_entry and not mapping.get("channel_description"):
                    desc = xm_entry.get("description", "")
                    if desc:
                        mappings[cid]["channel_description"] = desc
                        descs_fixed += 1

                if not mapping.get("xm_deeplink"):
                    if xm_entry:
                        uuid = xm_entry.get("lookaround_channel_id")
                        station = (_match_station_by_uuid(uuid, stations) if uuid else None) or \
                                  _match_station_by_name(channel_name, stations)
                    else:
                        station = _match_station_by_name(channel_name, stations)
                    if station:
                        deeplink = station.get("id")
                        mappings[cid]["xm_deeplink"] = deeplink
                        deeplinks_fixed += 1
                        logger.info(f"ticker: resolved deeplink for {channel_name} → {deeplink}")
                    else:
                        unmatched.append(channel_name)

            if deeplinks_fixed or descs_fixed:
                _save_mappings(mappings)

            mapped_deeplinks = {m.get("xm_deeplink") for m in mappings.values() if m.get("xm_deeplink")}
            orphan_stations = [s["name"] for s in stations if s.get("id") not in mapped_deeplinks]

            msg = (f"Fetched: {len(channels)} channel descriptions, {len(stations)} ticker.com channels.\n"
                   f"Fixed: {deeplinks_fixed} deeplink(s), {descs_fixed} description(s).\n"
                   f"Matched: {len(mapped_deeplinks)} of {len(stations)} ticker.com channels.")
            if orphan_stations:
                msg += f"\n\nTicker.com channels with no Dispatcharr match ({len(orphan_stations)}):\n" + ", ".join(orphan_stations[:20])
            if unmatched:
                msg += f"\n\nNo ticker.com match for {len(unmatched)} Dispatcharr channel(s) (first 20):\n" + ", ".join(unmatched[:20])
            return {"success": True, "message": msg}
        except Exception as e:
            return {"success": False, "message": f"Refresh failed: {e}"}

    # ------------------------------------------------------------------ #
    # Channel Management — Fill / Sort / Logos                           #
    # Mirrors EPGeditARR's structure exactly (no separate Order action). #
    # ------------------------------------------------------------------ #

    def _ch_resolve(self, params):
        """Return (channels_data, aliases, dispatch_channels, group_or_None) or raise ValueError."""
        from apps.channels.models import Channel, ChannelGroup
        channels_data, aliases = _get_channel_data()
        if not channels_data:
            raise ValueError("Channel data not available — run Refresh Channel Data first.")
        target_type = params.get("ch_target_type", "group")
        if target_type == "group":
            group_id = params.get("ch_channel_group_id")
            if not group_id:
                raise ValueError("Select a channel group in the Channel Setup section of Settings before running this action.")
            try:
                group = ChannelGroup.objects.get(id=int(group_id))
                return channels_data, aliases, list(
                    Channel.objects.filter(channel_group=group).order_by("channel_number", "name")
                ), group
            except ChannelGroup.DoesNotExist:
                raise ValueError("Channel group not found.")
        else:
            channel_id = params.get("ch_channel_id")
            if not channel_id:
                raise ValueError("Select a channel in the Channel Setup section of Settings before running this action.")
            try:
                return channels_data, aliases, [Channel.objects.get(id=int(channel_id))], None
            except Channel.DoesNotExist:
                raise ValueError("Channel not found.")

    def _do_fill(self, channel, xm_entry):
        """Set tvg_id to the official SiriusXM channel name for EPG source matching."""
        try:
            channel.tvg_id = xm_entry.get("name", channel.name)
            channel.save(update_fields=["tvg_id"])
            return True
        except Exception as e:
            logger.warning(f"ticker: fill failed for {channel.name}: {e}")
            return False

    def _do_logos(self, channel, xm_entry):
        """Assign logo from bundled data. Returns (ok, created)."""
        logo_url = xm_entry.get("logo_url", "")
        if not logo_url:
            return False, False
        return _assign_logo(channel, logo_url, xm_entry.get("name", channel.name))

    def _fill_epg(self, params):
        try:
            channels_data, aliases, dispatch_channels, _ = self._ch_resolve(params)
        except ValueError as e:
            return {"success": False, "message": str(e)}
        filled, skipped, failed = [], [], []
        for ch in dispatch_channels:
            # Strip any station-number prefix Sort may have already added — otherwise
            # a channel named "036 Alt Nation" fails to match "Alt Nation" here.
            match_name = re.sub(r'^\d+\s+', '', ch.name)
            xm = _match_channel(match_name, channels_data, aliases)
            if not xm:
                skipped.append(f"{ch.name} (no SiriusXM match)")
                continue
            if self._do_fill(ch, xm):
                filled.append(ch.name)
            else:
                failed.append(ch.name)
        parts = []
        if filled:  parts.append(f"Filled EPG TVG IDs: {len(filled)} channel(s)")
        if skipped: parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:  parts.append("Failed:\n"  + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _sort_channels(self, params):
        """Renumber channels from sort_start_number, ordered by SXM channel number.
        Sequential mode (default) packs channels with no gaps. Absolute mode sets
        channel_number = start_number + sxm_number, preserving gaps for missing
        stations so the trailing digits match the real SXM station number.
        Auto-detects start number from the current minimum channel_number if not configured."""
        from apps.channels.models import Channel
        try:
            channels_data, aliases, dispatch_channels, group = self._ch_resolve(params)
        except ValueError as e:
            return {"success": False, "message": str(e)}

        start_raw = (params.get("sort_start_number") or "").strip()
        auto_detected = False
        if start_raw:
            try:
                start_number = int(start_raw)
            except (ValueError, TypeError):
                return {"success": False, "message": f"Invalid Sort Start Number: {start_raw!r} - enter a whole number or leave blank to auto-detect."}
        else:
            auto_detected = True
            nums = [ch.channel_number for ch in dispatch_channels if ch.channel_number is not None]
            start_number = int(min(nums)) if nums else 1

        numbering_mode = (params.get("sort_numbering_mode") or "sequential").strip()
        station_prefix = bool(params.get("sort_station_prefix"))
        station_prefix_format = (params.get("sort_station_prefix_format") or "zero_padded").strip()

        block_size = None
        if numbering_mode == "absolute":
            block_raw = (params.get("sort_block_size") or "").strip()
            if not block_raw:
                return {"success": False, "message": (
                    "Numbering Block Size is required when Numbering Mode is Absolute — set the range "
                    "you want to reserve for this group (e.g. 1000) so unmatched channels can't drift "
                    "into whatever you numbered next."
                )}
            try:
                block_size = int(block_raw)
                if block_size <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                return {"success": False, "message": f"Invalid Numbering Block Size: {block_raw!r} - enter a positive whole number."}

        # Build ordered list: matched channels sorted by SXM number, unmatched at end.
        # Strip any station-number prefix a previous Sort run may have added before
        # matching — otherwise a channel prefixed as "036 Alt Nation" fails to match
        # "Alt Nation" in the dataset on the next run and drifts to the unmatched tail.
        matched, unmatched = [], []
        for ch in dispatch_channels:
            match_name = re.sub(r'^\d+\s+', '', ch.name)
            xm = _match_channel(match_name, channels_data, aliases)
            if xm and xm.get("sxm_number"):
                matched.append((xm["sxm_number"], ch))
            else:
                unmatched.append(ch)
        matched.sort(key=lambda x: x[0])
        ordered = [ch for _, ch in matched] + unmatched

        # Null out all channel numbers first to avoid unique-within-group constraint conflicts
        if group:
            Channel.objects.filter(channel_group=group).update(channel_number=None)
        else:
            for ch in ordered:
                ch.channel_number = None
                ch.save(update_fields=["channel_number"])

        # Assign target numbers — pair each channel with its SXM station number (if any)
        assignments = []  # (channel, new_number, sxm_number-or-None)
        out_of_block, overflowed, duplicate_matches = [], [], []
        if numbering_mode == "absolute":
            used = set()
            seen_numbers = {}
            extra_unmatched = []  # duplicate matches lose the number race, join the fill pool
            for sxm_number, ch in matched:
                if sxm_number in seen_numbers:
                    duplicate_matches.append((ch.name, sxm_number, seen_numbers[sxm_number]))
                    extra_unmatched.append(ch)
                    continue
                seen_numbers[sxm_number] = ch.name
                new_num = start_number + sxm_number
                assignments.append((ch, new_num, sxm_number))
                used.add(new_num)
                if sxm_number >= block_size:
                    out_of_block.append((ch.name, sxm_number))

            # Unmatched channels (plus any duplicate-match losers above) fill free slots
            # starting right after the highest real match, not from the bottom of the
            # block — low absolute numbers are exactly where more real SXM stations are
            # most likely to exist (SXM's own numbering starts at 2 and is densest in
            # the low range), so backfilling gaps like start+0/start+1 with unrelated
            # placeholder content put them ahead of genuinely-numbered channels.
            pool = unmatched + extra_unmatched
            floor = max(used) + 1 if used else start_number
            free_slots = [n for n in range(floor, start_number + block_size) if n not in used]
            for i, ch in enumerate(pool):
                if i < len(free_slots):
                    assignments.append((ch, free_slots[i], None))
                    used.add(free_slots[i])
                else:
                    overflowed.append(ch)
            next_num = start_number + block_size
            for ch in overflowed:
                while next_num in used:
                    next_num += 1
                assignments.append((ch, next_num, None))
                used.add(next_num)
        else:
            for i, ch in enumerate(ordered):
                sxm_number = matched[i][0] if i < len(matched) else None
                assignments.append((ch, start_number + i, sxm_number))

        updated, failed = 0, []
        for ch, new_num, sxm_number in assignments:
            try:
                update_fields = ["channel_number"]
                ch.channel_number = new_num
                if station_prefix and sxm_number:
                    base_name = re.sub(r'^\d+\s+', '', ch.name)
                    number_str = f"{sxm_number:03d}" if station_prefix_format == "zero_padded" else str(sxm_number)
                    new_name = f"{number_str} {base_name}"
                    if new_name != ch.name:
                        ch.name = new_name
                        update_fields.append("name")
                ch.save(update_fields=update_fields)
                updated += 1
            except Exception as e:
                logger.warning(f"ticker: sort failed for {ch.name}: {e}")
                failed.append(ch.name)

        start_note = " (auto-detected)" if auto_detected else ""
        mode_note = f" [absolute, block size {block_size}]" if numbering_mode == "absolute" else ""
        parts = [f"Sort complete — {updated} channel(s) renumbered from {start_number}{start_note}{mode_note}"]
        if unmatched: parts.append(f"No SXM match (placed at end): {', '.join(c.name for c in unmatched[:10])}")
        if duplicate_matches:
            parts.append(
                "Duplicate channels matched the same station — only the first kept the real "
                "absolute number, the rest were placed in the fill pool instead of colliding:\n"
                + "\n".join(f"  - {name} (station {num}, same as {first})" for name, num, first in duplicate_matches[:10])
            )
        if out_of_block:
            parts.append(
                "Real station number outside the block (still given its correct absolute number, not moved):\n"
                + "\n".join(f"  - {name} (station {num})" for name, num in out_of_block[:10])
            )
        if overflowed:
            parts.append(
                f"{len(overflowed)} unmatched channel(s) didn't fit inside the block and were placed past it — "
                f"increase Numbering Block Size to bring them back in range:\n"
                + "\n".join(f"  - {c.name}" for c in overflowed[:10])
            )
        if failed:    parts.append("Failed:\n" + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts)}

    def _assign_logos(self, params):
        try:
            channels_data, aliases, dispatch_channels, _ = self._ch_resolve(params)
        except ValueError as e:
            return {"success": False, "message": str(e)}
        assigned, skipped, failed = [], [], []
        for ch in dispatch_channels:
            # Strip any station-number prefix Sort may have already added — otherwise
            # a channel named "036 Alt Nation" fails to match "Alt Nation" here.
            match_name = re.sub(r'^\d+\s+', '', ch.name)
            xm = _match_channel(match_name, channels_data, aliases)
            if not xm:
                skipped.append(f"{ch.name} (no SiriusXM match)")
                continue
            ok, created = self._do_logos(ch, xm)
            if ok:
                assigned.append(f"{ch.name}" + (" (new)" if created else ""))
            elif not xm.get("logo_url"):
                skipped.append(f"{ch.name} (no logo in channel data)")
            else:
                failed.append(ch.name)
        parts = []
        if assigned: parts.append(f"Assigned logos: {len(assigned)} channel(s)")
        if skipped:  parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:   parts.append("Failed:\n"  + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _fill_sxm_epg(self, params):
        """Download Ticker's own SiriusXM XMLTV and import EPG data into Dispatcharr."""
        import xml.etree.ElementTree as ET
        import io
        import gc
        from datetime import datetime, timedelta, timezone
        from apps.epg.models import EPGSource, EPGData, ProgramData
        from django.db import transaction

        try:
            channels_data, aliases, dispatch_channels, _ = self._ch_resolve(params)
        except ValueError as e:
            return {"success": False, "message": str(e)}

        # Download Ticker's own hosted XMLTV
        xml_bytes = None
        last_err = None
        for _attempt in range(2):
            try:
                req = urllib.request.Request(TICKER_SXM_EPG_URL, headers={"User-Agent": "Ticker/0.1"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    xml_bytes = r.read()
                break
            except Exception as e:
                last_err = e
        if xml_bytes is None:
            return {"success": False, "message": f"Failed to download SiriusXM EPG data: {last_err}"}

        # Strip control characters invalid in XML 1.0
        xml_bytes = re.sub(rb'[\x00-\x08\x0b\x0c\x0e-\x1f]', b'', xml_bytes)

        sxm_src, _ = EPGSource.objects.get_or_create(
            name=TICKER_SXM_SOURCE,
            defaults={"source_type": "xmltv", "url": TICKER_SXM_EPG_URL, "refresh_interval": 24},
        )
        # Backfill: sources created before this field was set default to 0 (disabled),
        # which leaves Dispatcharr's periodic refresh task permanently off.
        if sxm_src.refresh_interval != 24:
            sxm_src.refresh_interval = 24
            sxm_src.save(update_fields=["refresh_interval"])

        now = datetime.now(timezone.utc)
        purge_before = now - timedelta(days=1)

        def _parse_dt(s):
            s = s.strip()
            dt = datetime.strptime(s[:14], "%Y%m%d%H%M%S")
            tz_str = s[14:].strip()
            if tz_str:
                sign = 1 if tz_str[0] == '+' else -1
                offset = timedelta(hours=int(tz_str[1:3]), minutes=int(tz_str[3:5])) * sign
            else:
                offset = timedelta(0)
            return (dt - offset).replace(tzinfo=timezone.utc)

        # Phase 1: create/update EPGData records from <channel> elements
        existing_epg = {e.tvg_id: e for e in EPGData.objects.filter(epg_source=sxm_src)}
        channel_map = {}
        with transaction.atomic():
            for _ev, elem in ET.iterparse(io.BytesIO(xml_bytes), events=('end',)):
                if elem.tag == 'channel':
                    ch_id = elem.get('id', '').strip()
                    if ch_id:
                        display = (elem.findtext('display-name') or ch_id).strip()
                        icon_el = elem.find('icon')
                        icon_url = (icon_el.get('src', '') if icon_el is not None else '').strip()
                        if ch_id in existing_epg:
                            entry = existing_epg[ch_id]
                            changed = []
                            if entry.name != display: entry.name = display; changed.append('name')
                            if entry.icon_url != icon_url: entry.icon_url = icon_url; changed.append('icon_url')
                            if changed: entry.save(update_fields=changed)
                        else:
                            entry = EPGData.objects.create(tvg_id=ch_id, name=display, icon_url=icon_url, epg_source=sxm_src)
                        channel_map[ch_id] = entry
                    elem.clear()
                elif elem.tag == 'programme':
                    elem.clear()

        # Phase 2: import ProgramData from <programme> elements
        total_programs = 0
        with transaction.atomic():
            ProgramData.objects.filter(epg__epg_source=sxm_src, end_time__lt=purge_before).delete()
            ProgramData.objects.filter(epg__epg_source=sxm_src, start_time__gte=now).delete()
            batch = []
            for _ev, elem in ET.iterparse(io.BytesIO(xml_bytes), events=('end',)):
                if elem.tag == 'channel':
                    elem.clear(); continue
                if elem.tag != 'programme':
                    continue
                ch_id = elem.get('channel', '').strip()
                entry = channel_map.get(ch_id)
                if entry:
                    try:
                        start = _parse_dt(elem.get('start', ''))
                        end   = _parse_dt(elem.get('stop', ''))
                    except Exception:
                        elem.clear(); continue
                    if end > purge_before:
                        batch.append(ProgramData(
                            epg=entry,
                            start_time=start, end_time=end,
                            title=(elem.findtext('title') or '').strip(),
                            sub_title=(elem.findtext('sub-title') or '').strip() or None,
                            description=(elem.findtext('desc') or '').strip(),
                            tvg_id=ch_id, custom_properties={},
                        ))
                        if len(batch) >= 2000:
                            ProgramData.objects.bulk_create(batch)
                            total_programs += len(batch)
                            batch = []
                elem.clear()
            if batch:
                ProgramData.objects.bulk_create(batch)
                total_programs += len(batch)

        del xml_bytes

        # Fuzzy-match dispatch channels to EPGData and assign channel.epg_data
        epg_lookup = {}
        for entry in channel_map.values():
            norm = _normalize(entry.name)
            epg_lookup[norm] = entry

        matched, unmatched = 0, []
        with transaction.atomic():
            for ch in dispatch_channels:
                # Strip any station-number prefix Sort may have already added —
                # otherwise a channel named "036 Alt Nation" fails to match here.
                match_name = re.sub(r'^\d+\s+', '', ch.name)
                key = _normalize(match_name)
                xm = _match_channel(match_name, channels_data, aliases)
                # Try alias-resolved name first, then direct
                best = (epg_lookup.get(_normalize(xm["name"])) if xm else None) or epg_lookup.get(key)
                if best:
                    if ch.epg_data_id != best.id:
                        ch.epg_data = best
                        ch.save(update_fields=['epg_data'])
                    matched += 1
                else:
                    unmatched.append(ch.name)

        n_ch = len(channel_map)
        del channel_map, epg_lookup
        gc.collect()

        sxm_src.status = "success"
        sxm_src.last_message = f"Ticker: {n_ch:,} channels, {total_programs:,} programs"
        sxm_src.save(update_fields=["status", "last_message"])

        lines = [
            f"SiriusXM Fill EPG complete\n",
            f"  Channels assigned : {matched:,} / {len(dispatch_channels):,}",
            f"  Programs loaded   : {total_programs:,}  ({n_ch:,} XMLTV channels)",
        ]
        if unmatched:
            lines.append(f"  No match ({len(unmatched)}): " + ", ".join(unmatched[:10]))
        return {"success": True, "message": "\n".join(lines)}

    def _fill_and_sort(self, params):
        r1 = self._fill_sxm_epg(params)
        r2 = self._sort_channels(params)
        msg = "\n\n".join(filter(None, [r1["message"], r2["message"]]))
        return {"success": r1["success"] and r2["success"], "message": msg}

    def _fill_sort_logos(self, params):
        r1 = self._fill_sxm_epg(params)
        r2 = self._sort_channels(params)
        r3 = self._assign_logos(params)
        msg = "\n\n".join(filter(None, [r1["message"], r2["message"], r3["message"]]))
        return {"success": all(r["success"] for r in [r1, r2, r3]), "message": msg}

    def _clean_orphans(self, params):
        from core.models import StreamProfile
        mappings = _get_mappings()
        active_ticker_ids = set()
        for m in mappings.values():
            for key in ("ticker_profile_id", "eas_profile_id"):
                if m.get(key):
                    active_ticker_ids.add(m[key])

        # Primary sweep: profiles whose name starts with PROFILE_PREFIX
        named = list(StreamProfile.objects.filter(name__startswith=PROFILE_PREFIX))
        orphans = [p for p in named if p.id not in active_ticker_ids]

        # Secondary sweep: catch FIFO-era leftovers whose name didn't use the current
        # prefix (different dash, older naming) but whose parameters contain ticker_data
        seen_ids = {p.id for p in named}
        for p in StreamProfile.objects.all():
            if p.id in seen_ids or p.id in active_ticker_ids:
                continue
            if "ticker_data" in (p.parameters or ""):
                orphans.append(p)

        if not orphans:
            return {"success": True, "message": "No orphaned profiles found."}
        deleted = []
        for profile in orphans:
            try:
                profile.delete()
                deleted.append(profile.name)
            except Exception as e:
                logger.warning(f"ticker: could not delete profile {profile.name}: {e}")
        return {"success": True, "message": f"Deleted {len(deleted)} orphaned profile(s):\n" + "\n".join(f"  - {n}" for n in deleted)}

    def _migrate_shared_profiles(self, params):
        """Move already-enabled channels off their legacy per-channel cloned profile onto a
        shared profile, now that {channelId} is available. Idempotent — channels already on
        a shared profile (or with no profile currently assigned, e.g. idle on-demand) are
        left alone. EAS/EAS-CA are not migrated here: those profiles only exist while an
        alert is actively firing and are already rebuilt from scratch on every new alert
        activation, so they migrate to shared profiles automatically on their next alert.
        """
        if not _supports_channel_id_token():
            return {"success": False, "message": (
                "This Dispatcharr install does not support the {channelId} token "
                "(requires v0.29.0+, Dispatcharr issue #1252). Nothing to migrate — "
                "channels will keep using one cloned stream profile per channel."
            )}

        from apps.channels.models import Channel
        from core.models import StreamProfile

        mappings = _get_mappings()
        migrated, skipped, failed = [], [], []
        old_pids_touched = set()

        for cid, mapping in list(mappings.items()):
            ticker_pid = mapping.get("ticker_profile_id")
            if not ticker_pid:
                continue  # nothing currently assigned — e.g. idle on-demand channel
            name = mapping.get("channel_name", f"channel {cid}")
            try:
                existing = StreamProfile.objects.filter(id=ticker_pid).first()
                if not existing or " [shared:" in existing.name:
                    continue  # already shared, or the clone is already gone — nothing to do

                channel = Channel.objects.filter(id=int(cid)).first()
                if not channel:
                    skipped.append(f"{name} (channel no longer exists)")
                    continue
                orig = StreamProfile.objects.filter(id=mapping.get("original_profile_id")).first()
                if not orig:
                    skipped.append(f"{name} (original base profile missing)")
                    continue

                ticker_type = mapping.get("type", "nowplaying")
                if ticker_type == "nowplaying":
                    new_profile, _ = _clone_and_inject(channel.id, orig, name)
                elif ticker_type == "custom":
                    new_profile, _ = _clone_and_inject_custom(
                        channel.id, orig, name,
                        mapping.get("custom_style", "static"),
                        mapping.get("custom_position", "bottom"),
                        mapping.get("custom_schedule", "always"),
                        mapping.get("custom_duration", 10),
                        mapping.get("custom_interval", 5),
                    )
                elif ticker_type == "sports":
                    new_profile, _ = _clone_and_inject_sports(
                        channel.id, orig, name,
                        mapping.get("sports_position", "bottom"),
                        int(mapping.get("sports_fontsize") or 36),
                        mapping.get("sports_labelcolor", "#ffd700"),
                        mapping.get("sports_abbrcolor", "#00d4ff"),
                        mapping.get("sports_color_mode", "single"),
                        mapping.get("sports_transcode_mode_video", "1080p30"),
                        mapping.get("sports_ticker_style", "scrolling"),
                    )
                else:
                    skipped.append(f"{name} ({ticker_type} — migrates automatically on its next alert)")
                    continue

                _assign_profile(channel, new_profile)
                _restart_channel_stream_async(channel, "Migrate")
                old_pids_touched.add(ticker_pid)
                mapping["ticker_profile_id"] = new_profile.id
                mappings[cid] = mapping
                migrated.append(name)
            except Exception as e:
                logger.error(f"ticker: migrate_shared_profiles failed for {name}: {e}", exc_info=True)
                failed.append(f"{name} (error: {e})")

        # Only delete a legacy profile once nothing in the (post-migration) mappings still
        # points at it — a legacy profile could in principle be shared by more than one
        # channel (e.g. hand-edited), and deleting it mid-loop would SET_NULL every other
        # channel still assigned to it (Channel.stream_profile is on_delete=SET_NULL) before
        # their turn to migrate came up.
        if old_pids_touched:
            still_referenced = set()
            for m in mappings.values():
                for key in ("ticker_profile_id", "eas_profile_id"):
                    if m.get(key) in old_pids_touched:
                        still_referenced.add(m[key])
            for old_pid in old_pids_touched - still_referenced:
                _delete_cloned_profile(old_pid)

        if migrated:
            _save_mappings(mappings)

        parts = []
        if migrated: parts.append(f"Migrated to shared profiles: {len(migrated)} channel(s)\n" + "\n".join(f"  - {n}" for n in migrated))
        if skipped:  parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:   parts.append("Failed:\n" + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or
                "Nothing to migrate — every channel is already on a shared profile (or has none assigned right now)."}

    def _migrate_from_tickarr(self, params):
        """One-time bridge migration for users upgrading from the pre-v0.5.00 "Tickarr"
        branding (full rename, not a fork — old and new plugin can be installed side by
        side, see CURRENT.md). Reads the legacy plugin's own mappings.json directly off
        disk, re-clones each active overlay under this plugin's own data dir/paths (the
        old clones point ffmpeg at tickarr_data/tickers/..., which nothing will be writing
        to anymore once Tickarr is removed), and imports every mapping — including ones
        with no currently-active clone (idle on-demand channels) — as-is.
        On a clean run (no failures) this also tears down the legacy install's own data:
        its mappings/caches directory and any of its cloned stream profiles that are no
        longer actually in use. That leaves uninstalling the Tickarr plugin itself (a
        Dispatcharr-side action this plugin has no access to) as the only manual step.
        If anything fails, nothing is torn down — safe to fix the issue and re-run.
        """
        from apps.channels.models import Channel
        from core.models import StreamProfile

        legacy_path, legacy_mappings = _find_legacy_mappings()
        if not legacy_path:
            return {"success": False, "message": (
                "No legacy Tickarr installation found (looked for "
                f"{LEGACY_DATA_DIR_NAME}/mappings.json next to this plugin). "
                "Nothing to migrate."
            )}
        if legacy_mappings is None:
            return {"success": False, "message": f"Found a legacy Tickarr install but could not read its mappings ({legacy_path}). See the log for details."}
        if not legacy_mappings:
            return {"success": False, "message": "Found a legacy Tickarr install, but it has no configured channels. Nothing to migrate."}

        mappings = _get_mappings()
        migrated, skipped, failed = [], [], []
        legacy_pids_touched = set()

        for cid, old in legacy_mappings.items():
            name = old.get("channel_name", f"channel {cid}")
            if cid in mappings:
                skipped.append(f"{name} (already configured under Ticker — skipped)")
                continue
            try:
                channel = Channel.objects.filter(id=int(cid)).first()
                if not channel:
                    skipped.append(f"{name} (channel no longer exists)")
                    continue

                ticker_type = old.get("type", "nowplaying")
                old_pid = old.get("ticker_profile_id")
                new_pid = None

                if old_pid:
                    orig = StreamProfile.objects.filter(id=old.get("original_profile_id")).first()
                    if not orig:
                        skipped.append(f"{name} (original base profile missing — active overlay could not be migrated)")
                        continue

                    if ticker_type == "nowplaying":
                        new_profile, _ = _clone_and_inject(channel.id, orig, name)
                    elif ticker_type == "custom":
                        new_profile, _ = _clone_and_inject_custom(
                            channel.id, orig, name,
                            old.get("custom_style", "static"),
                            old.get("custom_position", "bottom"),
                            old.get("custom_schedule", "always"),
                            old.get("custom_duration", 10),
                            old.get("custom_interval", 5),
                        )
                    elif ticker_type == "sports":
                        new_profile, _ = _clone_and_inject_sports(
                            channel.id, orig, name,
                            old.get("sports_position", "bottom"),
                            int(old.get("sports_fontsize") or 36),
                            old.get("sports_labelcolor", "#ffd700"),
                            old.get("sports_abbrcolor", "#00d4ff"),
                            old.get("sports_color_mode", "single"),
                            old.get("sports_transcode_mode_video", "1080p30"),
                            old.get("sports_ticker_style", "scrolling"),
                        )
                    else:
                        new_profile = None  # EAS-only entries never have ticker_profile_id set

                    if new_profile:
                        _assign_profile(channel, new_profile)
                        _restart_channel_stream_async(channel, "Migrate from Tickarr")
                        new_pid = new_profile.id
                        legacy_pids_touched.add(old_pid)

                new_mapping = dict(old)
                new_mapping["ticker_profile_id"] = new_pid
                mappings[cid] = new_mapping
                migrated.append(name)
            except Exception as e:
                logger.error(f"ticker: migrate_from_tickarr failed for {name}: {e}", exc_info=True)
                failed.append(f"{name} (error: {e})")

        if migrated:
            _save_mappings(mappings)

        parts = []
        if migrated: parts.append(f"Migrated: {len(migrated)} channel(s)\n" + "\n".join(f"  - {n}" for n in migrated))
        if skipped:  parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:   parts.append("Failed:\n" + "\n".join(f"  - {f}" for f in failed))

        if failed:
            parts.append(
                "Not tearing down the old Tickarr install because of the failures above — "
                "fix the issue and re-run this action. Already-migrated channels above are "
                "safe and won't be touched again."
            )
            return {"success": False, "message": "\n\n".join(parts)}

        # Clean run — tear down everything Tickarr left behind. A profile is only ever
        # deleted here if nothing (the mappings we just saved, or a live Channel FK) still
        # actually points at it — same safety check as Clean Orphaned Profiles.
        active_ids = set()
        for m in mappings.values():
            for key in ("ticker_profile_id", "eas_profile_id"):
                if m.get(key):
                    active_ids.add(m[key])

        torn_down = []
        for profile in StreamProfile.objects.filter(name__startswith=LEGACY_PROFILE_PREFIX):
            if profile.id in active_ids:
                continue
            if Channel.objects.filter(stream_profile_id=profile.id).exists():
                continue
            try:
                profile.delete()
                torn_down.append(profile.name)
            except Exception as e:
                logger.warning(f"ticker: could not delete legacy profile {profile.name}: {e}")

        try:
            shutil.rmtree(os.path.join(_PLUGINS_DIR, LEGACY_DATA_DIR_NAME))
        except OSError as e:
            logger.warning(f"ticker: could not remove legacy data dir {LEGACY_DATA_DIR_NAME}: {e}")
            parts.append(f"Note: could not remove the old {LEGACY_DATA_DIR_NAME} directory automatically ({e}) — safe to delete by hand.")

        if torn_down:
            parts.append(f"Removed {len(torn_down)} leftover Tickarr stream profile(s).")
        parts.append("Migration complete. You can now uninstall the Tickarr plugin — nothing else references it.")

        return {"success": True, "message": "\n\n".join(parts)}

    def _check_channel_id_support(self, params):
        supported = _supports_channel_id_token()
        if supported:
            return {"success": True, "message": (
                "Supported — this Dispatcharr install's StreamProfile.build_command() "
                "accepts channel_id, so the {channelId} stream profile substitution token "
                "(Dispatcharr issue #1252, shipped in v0.29.0) is available here. Ticker will "
                "use one shared stream profile per overlay type (per distinct combination of "
                "base profile + settings) instead of cloning one per channel."
            )}
        return {"success": False, "message": (
            "Not supported — this Dispatcharr install's StreamProfile.build_command() does "
            "not accept channel_id, so the {channelId} token (Dispatcharr issue #1252) is "
            "unavailable. Upgrade to Dispatcharr v0.29.0 or later to use it. Ticker will "
            "keep using one cloned stream profile per channel on this install."
        )}

    def _redis_diag(self, params):
        rc = _get_redis_client()
        if rc is None:
            return {"success": False, "message": (
                "Redis unavailable — could not connect.\n\n"
                "Stream-start detection is disabled. The sweep loop will poll all active "
                "channels every 15 seconds (one bulk stellartunerlog.com fetch per cycle)."
            )}

        lines = ["Redis: connected\n"]

        # Scan for active stream keys (both v0.24 and v0.25 patterns)
        try:
            ts_keys = (list(rc.scan_iter("live:channel:*", count=500)) +
                       list(rc.scan_iter("ts_proxy:channel:*", count=500)))
            if ts_keys:
                lines.append(f"Stream keys ({len(ts_keys)} found):")
                for raw in ts_keys[:40]:
                    k = raw.decode() if isinstance(raw, bytes) else raw
                    try:
                        ktype = rc.type(raw).decode()
                        if ktype == "set":
                            n = rc.scard(raw)
                            lines.append(f"  {k}  [set, {n} member(s)]")
                        elif ktype == "string":
                            v = (rc.get(raw) or b"").decode(errors="replace")
                            lines.append(f"  {k}  [string: {v[:80]}]")
                        else:
                            lines.append(f"  {k}  [{ktype}]")
                    except Exception as ex:
                        lines.append(f"  {k}  [error: {ex}]")
                if len(ts_keys) > 40:
                    lines.append(f"  ... and {len(ts_keys) - 40} more")
            else:
                lines.append("No stream keys found - scanning ALL keys to find active stream pattern:\n")
                try:
                    all_keys = list(rc.scan_iter("*", count=500))
                    if all_keys:
                        lines.append(f"All Redis keys ({len(all_keys)} total, showing first 60):")
                        for raw in all_keys[:60]:
                            k = raw.decode() if isinstance(raw, bytes) else raw
                            try:
                                ktype = rc.type(raw).decode()
                                if ktype == "string":
                                    v = (rc.get(raw) or b"").decode(errors="replace")
                                    lines.append(f"  {k}  [string: {v[:60]}]")
                                elif ktype == "set":
                                    n = rc.scard(raw)
                                    lines.append(f"  {k}  [set, {n} member(s)]")
                                else:
                                    lines.append(f"  {k}  [{ktype}]")
                            except Exception:
                                lines.append(f"  {k}")
                        if len(all_keys) > 60:
                            lines.append(f"  ... and {len(all_keys) - 60} more")
                    else:
                        lines.append("  Redis is empty - no keys at all.")
                except Exception as e2:
                    lines.append(f"  Full scan error: {e2}")
        except Exception as e:
            lines.append(f"ts_proxy scan error: {e}")

        # Show what _redis_scan_active() currently returns
        active = _redis_scan_active()
        if active is None:
            lines.append("\n_redis_scan_active(): returned None (error during scan)")
        else:
            mappings = _get_mappings()
            ch_list = _build_channel_list(mappings)
            mapped_ids = {c[0] for c in ch_list}
            matched = active & mapped_ids
            lines.append(f"\n_redis_scan_active(): {len(active)} active channel ID(s) total, "
                         f"{len(matched)} matching mapped channels")
            if matched:
                id_to_name = {c[0]: c[2] for c in ch_list}
                for cid in sorted(matched):
                    lines.append(f"  - [{cid}] {id_to_name.get(cid, '?')}")

        # Show which worker PID currently owns the background poller loops
        try:
            from apps.channels.models import RedisClient
            rc = RedisClient.get_client()
            owner = rc.get(_POLLER_LOCK_KEY)
            owner_str = owner.decode() if isinstance(owner, bytes) else owner
            ttl = rc.ttl(_POLLER_LOCK_KEY)
            lines.append(f"\npoller lock: owned by PID {owner_str or '(none)'}, expires in {ttl}s"
                         if owner_str else "\npoller lock: not currently held (will be claimed on next tick)")
        except Exception as e:
            lines.append(f"\npoller lock check error: {e}")

        return {"success": True, "message": "\n".join(lines)}

    def _reload_poller(self, params):
        global _scheduler_thread, _stop_event
        _stop_event.set()
        if _scheduler_thread:
            _scheduler_thread.join(timeout=5)
        _stop_event = threading.Event()
        _scheduler_thread = threading.Thread(
            target=_poll_loop,
            args=(_stop_event,),
            daemon=True,
            name="ticker-poller",
        )
        _scheduler_thread.start()
        logger.info("ticker: poller thread reloaded via action")
        return {"success": True, "message": "Poller thread restarted. Live data will resume within 15 seconds."}

    def _restart_dispatcharr(self, params):
        import signal as _signal

        def _do_restart():
            time.sleep(2)
            try:
                result = subprocess.run(
                    ["pgrep", "-of", "gunicorn"],
                    capture_output=True, text=True
                )
                pid = int(result.stdout.strip())
                logger.info(f"ticker: sending SIGHUP to gunicorn master PID {pid}")
                os.kill(pid, _signal.SIGHUP)
            except Exception as e:
                logger.warning(f"ticker: gunicorn SIGHUP failed ({e}), falling back to PID 1")
                try:
                    os.kill(1, _signal.SIGHUP)
                except Exception as e2:
                    logger.error(f"ticker: restart failed: {e2}")

        threading.Thread(target=_do_restart, daemon=True).start()
        return {"success": True, "message": "Restart signal sent. Dispatcharr will reload in ~2 seconds.\n\nRefresh this page in about 15 seconds."}

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _resolve_channels(self, params, prefix=""):
        from apps.channels.models import Channel, ChannelGroup
        target_type = params.get(f"{prefix}target_type", "group")

        # Build exclusion set from the exclude_groups field (applies to all target types)
        exclude_ids = set()
        raw_exclude = params.get(f"{prefix}exclude_groups", "")
        if raw_exclude:
            for name in [n.strip() for n in raw_exclude.split(",") if n.strip()]:
                try:
                    grp = ChannelGroup.objects.get(name__iexact=name)
                    exclude_ids.update(
                        Channel.objects.filter(channel_group=grp).values_list("id", flat=True)
                    )
                except ChannelGroup.DoesNotExist:
                    pass

        def _apply_exclusions(channels):
            if not exclude_ids:
                return channels
            return [ch for ch in channels if ch.id not in exclude_ids]

        if target_type == "all":
            return _apply_exclusions(list(Channel.objects.all().order_by("name")))

        if target_type == "group":
            group_id = params.get(f"{prefix}channel_group_id")
            if not group_id:
                return []
            try:
                group = ChannelGroup.objects.get(id=int(group_id))
                return _apply_exclusions(list(Channel.objects.filter(channel_group=group).order_by("name")))
            except ChannelGroup.DoesNotExist:
                return []

        if target_type == "groups":
            raw = params.get(f"{prefix}channel_group_names", "")
            names = [n.strip() for n in raw.split(",") if n.strip()]
            if not names:
                return []
            channels = []
            seen = set()
            for name in names:
                try:
                    group = ChannelGroup.objects.get(name__iexact=name)
                    for ch in Channel.objects.filter(channel_group=group).order_by("name"):
                        if ch.id not in seen:
                            seen.add(ch.id)
                            channels.append(ch)
                except ChannelGroup.DoesNotExist:
                    pass
            return _apply_exclusions(channels)

        # single channel
        channel_id = params.get(f"{prefix}channel_id")
        if not channel_id:
            return []
        try:
            ch = Channel.objects.get(id=int(channel_id))
            return [] if ch.id in exclude_ids else [ch]
        except Channel.DoesNotExist:
            return []
