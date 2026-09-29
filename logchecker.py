"""Formatting and MusicBrainz enrichment for the /log command.

The logcheck service identifies the disc table of contents.  MusicBrainz can then
turn that Disc ID/TOC into release *candidates*.  A TOC is strong evidence, but it
is not a unique pressing identifier, so this module deliberately avoids calling a
candidate a guaranteed match.
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx


MUSICBRAINZ_API = "https://musicbrainz.org/ws/2/discid"
MUSICBRAINZ_USER_AGENT = (
    "AlfredAudioBot/1.0 (https://github.com/spideyonmoon/alfred-audio-bot)"
)
MAX_RENDERED_CANDIDATES = 5

_DISCID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_musicbrainz_lock = asyncio.Lock()
_last_musicbrainz_request = 0.0


@dataclass(frozen=True)
class ReleaseCandidate:
    """The pressing-level fields useful for distinguishing release candidates."""

    release_id: str
    title: str
    artist: str
    date: str
    area: str
    country: str
    status: str
    packaging: str
    barcode: str
    labels: tuple[tuple[str, str], ...]
    media: tuple[str, ...]
    exact_toc: bool

    @property
    def url(self) -> str:
        return f"https://musicbrainz.org/release/{quote(self.release_id, safe='')}"


class RichMessageError(RuntimeError):
    """A sanitized Bot API failure while publishing a rich log-check report."""


def _text(value: Any, default: str = "Unknown") -> str:
    if value is None:
        return default
    rendered = str(value).strip()
    return rendered or default


def _artist_credit(value: Any) -> str:
    if not isinstance(value, list):
        return "Unknown artist"
    parts: list[str] = []
    for credit in value:
        if not isinstance(credit, dict):
            continue
        artist = credit.get("artist") if isinstance(credit.get("artist"), dict) else {}
        name = _text(credit.get("name") or artist.get("name"), "")
        if name:
            parts.append(name + _text(credit.get("joinphrase"), ""))
    return "".join(parts).strip() or "Unknown artist"


def _release_area(release: dict[str, Any]) -> str:
    events = release.get("release-events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            area = event.get("area")
            if isinstance(area, dict) and area.get("name"):
                return _text(area["name"])
    return ""


def _release_has_discid(release: dict[str, Any], disc_id: str) -> bool:
    for medium in release.get("media") or []:
        if not isinstance(medium, dict):
            continue
        for disc in medium.get("discs") or []:
            if isinstance(disc, dict) and disc.get("id") == disc_id:
                return True
    return False


def parse_release_candidates(payload: Any, disc_id: str) -> list[ReleaseCandidate]:
    """Convert a MusicBrainz Disc ID response into stable display objects."""
    if not isinstance(payload, dict) or not isinstance(payload.get("releases"), list):
        return []

    candidates: list[ReleaseCandidate] = []
    for release in payload["releases"]:
        if not isinstance(release, dict) or not release.get("id"):
            continue

        labels: list[tuple[str, str]] = []
        for info in release.get("label-info") or []:
            if not isinstance(info, dict):
                continue
            label = info.get("label") if isinstance(info.get("label"), dict) else {}
            labels.append(
                (
                    _text(label.get("name"), "Unknown label"),
                    _text(info.get("catalog-number"), "None listed"),
                )
            )

        media: list[str] = []
        for medium in release.get("media") or []:
            if not isinstance(medium, dict):
                continue
            medium_format = _text(medium.get("format"), "Unknown format")
            track_count = medium.get("track-count")
            if isinstance(track_count, int):
                medium_format += f" ({track_count} tracks)"
            media.append(medium_format)

        candidates.append(
            ReleaseCandidate(
                release_id=_text(release.get("id"), ""),
                title=_text(release.get("title"), "Unknown release"),
                artist=_artist_credit(release.get("artist-credit")),
                date=_text(release.get("date"), "Date unknown"),
                area=_release_area(release),
                country=_text(release.get("country"), ""),
                status=_text(release.get("status"), "Status unknown"),
                packaging=_text(release.get("packaging"), "Packaging unknown"),
                barcode=_text(release.get("barcode"), "None listed"),
                labels=tuple(labels),
                media=tuple(media),
                exact_toc=_release_has_discid(release, disc_id),
            )
        )

    # Direct Disc ID associations are stronger than fuzzy TOC results.
    return sorted(
        candidates,
        key=lambda candidate: (
            not candidate.exact_toc,
            candidate.date == "Date unknown",
            candidate.date,
            candidate.country,
            candidate.release_id,
        ),
    )


def _toc_from_attach_url(url: Any) -> str | None:
    """Extract a safe MusicBrainz TOC from the attach URL returned by logcheck."""
    if not isinstance(url, str):
        return None
    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return None
    toc = (query.get("toc") or [None])[0]
    if not isinstance(toc, str):
        return None
    fields = toc.split()
    if len(fields) < 4 or any(not field.isdigit() for field in fields):
        return None
    return "+".join(fields)


async def fetch_release_candidates(
    client: httpx.AsyncClient,
    logcheck_data: dict[str, Any],
) -> list[ReleaseCandidate]:
    """Look up MusicBrainz candidates while respecting its one-call/second limit."""
    disc_id = _text(logcheck_data.get("musicbrainz_id"), "")
    if not disc_id or not _DISCID_RE.fullmatch(disc_id):
        return []

    params = {
        "inc": "artist-credits+labels+release-groups",
        "fmt": "json",
        "cdstubs": "no",
    }
    toc = _toc_from_attach_url(logcheck_data.get("musicbrainz_url"))
    if toc:
        # If the Disc ID is not yet attached, MusicBrainz uses this for a fuzzy TOC
        # search.  Exact versus fuzzy candidates are distinguished during parsing.
        params["toc"] = toc

    global _last_musicbrainz_request
    async with _musicbrainz_lock:
        delay = 1.05 - (time.monotonic() - _last_musicbrainz_request)
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            response = await client.get(
                f"{MUSICBRAINZ_API}/{quote(disc_id, safe='')}",
                params=params,
                headers={"User-Agent": MUSICBRAINZ_USER_AGENT, "Accept": "application/json"},
            )
        finally:
            _last_musicbrainz_request = time.monotonic()

    if response.status_code == 404:
        return []
    response.raise_for_status()
    return parse_release_candidates(response.json(), disc_id)


def _h(value: Any, default: str = "Unknown") -> str:
    return html.escape(_text(value, default))


def _safe_link(label: Any, url: Any) -> str:
    escaped_label = _h(label)
    if not isinstance(url, str):
        return escaped_label
    try:
        parsed = urlsplit(url)
    except ValueError:
        return escaped_label
    if parsed.scheme != "https" or not parsed.netloc:
        return escaped_label
    return f'<a href="{html.escape(url, quote=True)}">{escaped_label}</a>'


def _status(value: Any) -> str:
    text = _text(value, "Unknown")
    lowered = text.casefold()
    if lowered in {"found", "matched", "match", "ok"}:
        return f"✅ {_h(text)}"
    if "not found" in lowered or lowered in {"missing", "unmatched"}:
        return f"⚪ {_h(text)}"
    return f"❔ {_h(text)}"


def _database_lines(data: dict[str, Any]) -> list[str]:
    lines: list[str] = []

    mb_id = data.get("musicbrainz_id")
    if mb_id:
        mb_label = _safe_link(mb_id, data.get("musicbrainz_url"))
        lines.append(f"• <b>MusicBrainz Disc ID:</b> {mb_label}")

    ctdb_id = data.get("ctdb_id")
    if ctdb_id:
        ctdb_label = _safe_link(ctdb_id, data.get("ctdb_url"))
        lines.append(f"• <b>CTDB:</b> {ctdb_label}")

    accuraterip_id = data.get("accuraterip_id")
    accuraterip_status = data.get("accuraterip_status")
    if accuraterip_id or accuraterip_status:
        suffix = f" · <code>{_h(accuraterip_id)}</code>" if accuraterip_id else ""
        lines.append(f"• <b>AccurateRip:</b> {_status(accuraterip_status)}{suffix}")

    freedb_id = data.get("freedb_id")
    if freedb_id:
        lines.append(f"• <b>FreeDB:</b> <code>{_h(freedb_id)}</code>")

    gnudb_id = data.get("gnudb_id")
    gnudb_status = data.get("gnudb_status")
    gnudb_title = data.get("gnudb_title")
    if gnudb_id or gnudb_status or gnudb_title:
        identifier = _safe_link(gnudb_id, data.get("gnudb_url")) if gnudb_id else ""
        suffix = f" · {identifier}" if identifier else ""
        lines.append(f"• <b>gnudb:</b> {_status(gnudb_status)}{suffix}")
        if gnudb_title:
            lines.append(f"  <i>{_h(gnudb_title)}</i>")

    return lines


def _format_candidate(candidate: ReleaseCandidate, index: int, show_number: bool) -> str:
    heading = f"{index}. " if show_number else ""
    place = candidate.area or candidate.country
    if candidate.area and candidate.country:
        place = f"{candidate.area} ({candidate.country})"

    lines = [
        f"<b>{heading}{_h(candidate.artist)} — {_h(candidate.title)}</b>",
        f"📅 {_h(candidate.date)}" + (f" · {_h(place)}" if place else ""),
    ]
    if candidate.media:
        lines.append(f"💿 {_h(' · '.join(candidate.media))}")
    if candidate.labels:
        label_text = "; ".join(
            f"{label} · Cat# {catalog_number}" for label, catalog_number in candidate.labels
        )
        lines.append(f"🏷 {_h(label_text)}")
    else:
        lines.append("🏷 Label / Cat#: <i>None listed</i>")
    lines.extend(
        [
            f"🔢 Barcode: <code>{_h(candidate.barcode)}</code>",
            f"📦 {_h(candidate.packaging)} · {_h(candidate.status)}",
            f"🔗 {_safe_link('MusicBrainz release', candidate.url)}",
        ]
    )
    return "\n".join(lines)


def _score_presentation(score: Any) -> tuple[str, str]:
    if not isinstance(score, int):
        return "❓", _h(score)
    if score == 100:
        return "💯", _h(score)
    if score >= 80:
        return "🟢", _h(score)
    if score >= 50:
        return "🟡", _h(score)
    return "🔴", _h(score)


def _checksum_presentation(data: dict[str, Any]) -> str:
    labels = {
        "checksum_ok": "✅ OK",
        "checksum_missing": "⚠ Missing",
        "checksum_mismatch": "❌ Mismatch",
    }
    raw = _text(data.get("checksum_state"), "Unknown")
    return labels.get(raw, _h(raw))


def _rich_database_items(data: dict[str, Any]) -> list[str]:
    """Compact, link-first database rows for a rich-message disclosure."""
    items: list[str] = []
    if data.get("musicbrainz_id"):
        items.append(
            "<b>MusicBrainz</b> — "
            f"{_safe_link('Disc ID', data.get('musicbrainz_url'))} "
            f"<code>{_h(data['musicbrainz_id'])}</code>"
        )
    if data.get("ctdb_id"):
        items.append(
            "<b>CTDB</b> — "
            f"{_safe_link('Open record', data.get('ctdb_url'))} "
            f"<code>{_h(data['ctdb_id'])}</code>"
        )
    if data.get("accuraterip_id") or data.get("accuraterip_status"):
        identifier = f" · <code>{_h(data['accuraterip_id'])}</code>" if data.get("accuraterip_id") else ""
        items.append(f"<b>AccurateRip</b> — {_status(data.get('accuraterip_status'))}{identifier}")
    if data.get("freedb_id"):
        items.append(f"<b>FreeDB</b> — <code>{_h(data['freedb_id'])}</code>")
    if data.get("gnudb_id") or data.get("gnudb_status") or data.get("gnudb_title"):
        link = _safe_link('Open record', data.get('gnudb_url')) if data.get("gnudb_id") else ""
        title = f" · <i>{_h(data['gnudb_title'])}</i>" if data.get("gnudb_title") else ""
        items.append(f"<b>gnudb</b> — {_status(data.get('gnudb_status'))}" + (f" · {link}" if link else "") + title)
    return items


def _rich_candidate_card(candidate: ReleaseCandidate, index: int, is_open: bool) -> str:
    place = candidate.area or candidate.country
    if candidate.area and candidate.country:
        place = f"{candidate.area} ({candidate.country})"
    match = "Exact Disc ID match" if candidate.exact_toc else "Fuzzy TOC match"
    labels = "; ".join(f"{label} · Cat# {catalog}" for label, catalog in candidate.labels) or "None listed"
    media = " · ".join(candidate.media) or "Unknown format"
    open_attribute = " open" if is_open else ""
    place_text = f" · {_h(place)}" if place else ""

    return (
        f"<details{open_attribute}><summary><b>{index}. {_h(candidate.artist)} — {_h(candidate.title)}</b>"
        f" · {_h(candidate.date)}{place_text}</summary>"
        "<table compact>"
        f"<tr><td><b>Match</b></td><td>{_h(match)}</td></tr>"
        f"<tr><td><b>Format</b></td><td>{_h(media)}</td></tr>"
        f"<tr><td><b>Label</b></td><td>{_h(labels)}</td></tr>"
        f"<tr><td><b>Barcode</b></td><td><code>{_h(candidate.barcode)}</code></td></tr>"
        f"<tr><td><b>Edition</b></td><td>{_h(candidate.packaging)} · {_h(candidate.status)}</td></tr>"
        "</table>"
        f"<p>{_safe_link('View this release on MusicBrainz', candidate.url)}</p>"
        "</details>"
    )


def build_rich_logcheck_message(
    data: dict[str, Any],
    filename: str,
    candidates: list[ReleaseCandidate] | None = None,
    release_lookup_error: bool = False,
) -> dict[str, Any]:
    """Build a compact Bot API InputRichMessage for a /log result.

    The summary is intentionally short; IDs and alternative pressings live in
    native disclosure blocks so a report remains readable even with many matches.
    """
    candidates = candidates or []
    score_emoji, score_text = _score_presentation(data.get("score", "?"))
    ripper = " ".join(
        value for value in (_text(data.get("ripper"), "Unknown"), _text(data.get("ripper_version"), "")) if value
    )
    combined = "Combined log" if data.get("is_combined_log") is True else "Single-disc log" if data.get("is_combined_log") is False else "Log type unknown"
    details = data.get("details")

    blocks = [
        "<h3>Rip log report</h3>",
        f"<blockquote><b>{_h(filename)}</b><br/>{_h(ripper)} · {_h(data.get('language'))} · {combined}</blockquote>",
        "<table compact><caption>Integrity</caption>"
        f"<tr><td><b>Score</b></td><td>{score_emoji} <mark>{score_text} / 100</mark></td></tr>"
        f"<tr><td><b>Checksum</b></td><td>{_checksum_presentation(data)}</td></tr>"
        "</table>",
    ]

    if isinstance(details, list) and details:
        detail_html = "".join(f"<li>{_h(detail)}</li>" for detail in details)
        blocks.append(f"<details><summary><b>Checker details</b> · {len(details)}</summary><ul>{detail_html}</ul></details>")
    elif details:
        blocks.append(f"<details><summary><b>Checker details</b></summary><p>{_h(details)}</p></details>")

    database_items = _rich_database_items(data)
    if database_items:
        blocks.append(
            "<details><summary><b>Database checks</b> · "
            f"{len(database_items)} sources</summary><ul>"
            + "".join(f"<li>{item}</li>" for item in database_items)
            + "</ul></details>"
        )

    blocks.append("<hr/>")
    if candidates:
        exact_count = sum(candidate.exact_toc for candidate in candidates)
        heading = "Release candidate" if len(candidates) == 1 else f"Release candidates · {len(candidates)}"
        blocks.append(f"<h4>{heading}</h4>")
        for index, candidate in enumerate(candidates[:MAX_RENDERED_CANDIDATES], start=1):
            blocks.append(_rich_candidate_card(candidate, index, is_open=index == 1 and candidate.exact_toc))
        remaining = len(candidates) - MAX_RENDERED_CANDIDATES
        if remaining > 0:
            blocks.append(f"<p><i>{remaining} more candidate(s) are available on MusicBrainz.</i></p>")
        match_note = "Exact Disc ID match" if exact_count else "Fuzzy TOC match"
        if exact_count and exact_count != len(candidates):
            match_note += f" for {exact_count} of {len(candidates)} candidates"
        blocks.append(
            f"<footer>{match_note}. A CD TOC can be shared by multiple pressings; label, catalog number and barcode are candidate metadata.</footer>"
        )
    elif data.get("musicbrainz_id"):
        message = "Release metadata is temporarily unavailable." if release_lookup_error else "No MusicBrainz release candidate is attached to this Disc ID / TOC yet."
        blocks.append(f"<p><i>{message}</i></p>")

    return {"html": "".join(blocks), "skip_entity_detection": True}


async def send_rich_logcheck_message(
    client: httpx.AsyncClient,
    bot_token: str,
    chat_id: int,
    reply_to_message_id: int,
    message_thread_id: int | None,
    rich_message: dict[str, Any],
) -> None:
    """Publish a rich result through the Bot API, which Pyrofork does not expose."""
    if not bot_token:
        raise RichMessageError("Bot token is not configured")

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": rich_message,
        "reply_parameters": {
            "message_id": reply_to_message_id,
            "allow_sending_without_reply": True,
        },
    }
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id

    response = await client.post(
        f"https://api.telegram.org/bot{bot_token}/sendRichMessage",
        json=payload,
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or not isinstance(body, dict) or not body.get("ok"):
        description = body.get("description") if isinstance(body, dict) else None
        raise RichMessageError(_text(description, f"Telegram Bot API returned HTTP {response.status_code}"))


def format_logcheck_result(
    data: dict[str, Any],
    filename: str,
    candidates: list[ReleaseCandidate] | None = None,
    release_lookup_error: bool = False,
) -> str:
    """Render the complete Telegram HTML result, including all new API fields."""
    score = data.get("score", "?")
    score_emoji = "❓"
    if isinstance(score, int):
        if score == 100:
            score_emoji = "💯"
        elif score >= 80:
            score_emoji = "🟢"
        elif score >= 50:
            score_emoji = "🟡"
        else:
            score_emoji = "🔴"

    checksum_labels = {
        "checksum_ok": "✅ OK",
        "checksum_missing": "⚠ Missing",
        "checksum_mismatch": "❌ Mismatch",
    }
    checksum_raw = _text(data.get("checksum_state"), "Unknown")
    checksum = checksum_labels.get(checksum_raw, _h(checksum_raw))
    ripper = " ".join(
        value for value in (_text(data.get("ripper"), "Unknown"), _text(data.get("ripper_version"), "")) if value
    )
    combined = "Yes" if data.get("is_combined_log") is True else "No" if data.get("is_combined_log") is False else "Unknown"

    lines = [
        f"<blockquote><b>{_h(filename)}</b></blockquote>",
        "",
        f"{score_emoji} <b>Score:</b> <code>{_h(score)}/100</code>",
        f"🎙 <b>Ripper:</b> <code>{_h(ripper)}</code>",
        f"🔐 <b>Checksum:</b> {checksum}",
        f"🌐 <b>Language:</b> <code>{_h(data.get('language'))}</code>",
        f"📚 <b>Combined log:</b> {combined}",
    ]

    details = data.get("details")
    if isinstance(details, list):
        detail_lines = [f"  • {_h(detail)}" for detail in details]
    elif details:
        detail_lines = [f"  • {_h(details)}"]
    else:
        detail_lines = ["  <i>None</i>"]
    lines.extend(["", "<b>Details</b>", *detail_lines])

    database_lines = _database_lines(data)
    if database_lines:
        lines.extend(["", "<b>Database verification</b>", *database_lines])

    candidates = candidates or []
    if candidates:
        exact_count = sum(candidate.exact_toc for candidate in candidates)
        if len(candidates) == 1:
            title = "<b>Possible release</b>"
        else:
            title = f"<b>Possible releases ({len(candidates)})</b>"
        lines.extend(["", title])
        for index, candidate in enumerate(candidates[:MAX_RENDERED_CANDIDATES], start=1):
            if index > 1:
                lines.append("")
            lines.append(_format_candidate(candidate, index, len(candidates) > 1))
        remaining = len(candidates) - MAX_RENDERED_CANDIDATES
        if remaining > 0:
            lines.append(f"\n<i>…and {remaining} more candidate(s) on MusicBrainz.</i>")

        if exact_count:
            match_note = "Exact Disc ID / CD TOC match"
            if exact_count != len(candidates):
                match_note += f" for {exact_count} of {len(candidates)} candidates"
        else:
            match_note = "Fuzzy CD TOC match"
        lines.append(
            f"\n<i>{match_note}. A TOC may be shared by multiple pressings, so label, "
            "catalog number and barcode are candidate metadata—not absolute proof.</i>"
        )
    elif data.get("musicbrainz_id"):
        message = "Release metadata lookup was temporarily unavailable." if release_lookup_error else "No MusicBrainz release candidate is currently attached to this Disc ID / TOC."
        lines.extend(["", "<b>Possible release</b>", f"<i>{message}</i>"])

    return "\n".join(lines)
