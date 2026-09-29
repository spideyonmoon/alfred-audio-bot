import asyncio
import json
import unittest
from urllib.parse import parse_qs

import httpx

import logchecker
from logchecker import (
    build_rich_logcheck_message,
    fetch_release_candidates,
    format_logcheck_result,
    parse_release_candidates,
    send_rich_logcheck_message,
)


LOGCHECK_RESPONSE = {
    "ripper": "EAC",
    "ripper_version": "1.8",
    "score": 100,
    "checksum_state": "checksum_ok",
    "details": None,
    "language": "en",
    "is_combined_log": False,
    "musicbrainz_id": "TIhchfPn2J4Enqyr5GGHAD1n4H8-",
    "musicbrainz_url": "https://musicbrainz.org/cdtoc/attach?toc=1+17+331818+150+5651&tracks=17&id=TIhchfPn2J4Enqyr5GGHAD1n4H8-",
    "ctdb_id": "RS2HWtZD_FI062AwKiBcXL4g.Ec-",
    "ctdb_url": "https://db.cuetools.net/ui/?tocid=RS2HWtZD_FI062AwKiBcXL4g.Ec-",
    "freedb_id": "f5114611",
    "accuraterip_id": "017-002b1f27-022c64ed-f5114611",
    "accuraterip_status": "Found",
    "gnudb_id": "f5114698",
    "gnudb_url": "https://gnudb.org/cd/f5114698",
    "gnudb_status": "Matched",
    "gnudb_title": "Raye / This Music May Contain Hope",
}

MUSICBRAINZ_RESPONSE = {
    "id": LOGCHECK_RESPONSE["musicbrainz_id"],
    "releases": [
        {
            "id": "f7d8f88a-752d-4a93-b5d1-520018ba8891",
            "title": "THIS MUSIC MAY CONTAIN HOPE.",
            "artist-credit": [{"name": "RAYE", "joinphrase": ""}],
            "date": "2026-03-27",
            "country": "XE",
            "release-events": [{"area": {"name": "Europe"}}],
            "status": "Official",
            "packaging": "Jewel Case",
            "barcode": "199806975411",
            "label-info": [
                {
                    "label": {"name": "Human Re Sources"},
                    "catalog-number": "[none]",
                }
            ],
            "media": [
                {
                    "format": "CD",
                    "track-count": 17,
                    "discs": [{"id": LOGCHECK_RESPONSE["musicbrainz_id"]}],
                }
            ],
        }
    ],
}


class LogcheckerTests(unittest.TestCase):
    def test_parses_pressing_metadata_and_exact_match(self):
        candidates = parse_release_candidates(
            MUSICBRAINZ_RESPONSE, LOGCHECK_RESPONSE["musicbrainz_id"]
        )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertTrue(candidate.exact_toc)
        self.assertEqual(candidate.artist, "RAYE")
        self.assertEqual(candidate.labels, (("Human Re Sources", "[none]"),))
        self.assertEqual(candidate.barcode, "199806975411")
        self.assertEqual(candidate.media, ("CD (17 tracks)",))

    def test_formats_all_new_logcheck_fields_and_release(self):
        candidates = parse_release_candidates(
            MUSICBRAINZ_RESPONSE, LOGCHECK_RESPONSE["musicbrainz_id"]
        )
        result = format_logcheck_result(LOGCHECK_RESPONSE, "log2.log", candidates)

        for expected in (
            "Language:</b> <code>en</code>",
            "Combined log:</b> No",
            "MusicBrainz Disc ID",
            "CTDB",
            "AccurateRip",
            "FreeDB",
            "gnudb",
            "Human Re Sources · Cat# [none]",
            "199806975411",
            "Exact Disc ID / CD TOC match",
        ):
            self.assertIn(expected, result)

    def test_escapes_untrusted_api_and_filename_text(self):
        response = dict(LOGCHECK_RESPONSE)
        response["details"] = ["<b>not markup</b>"]
        result = format_logcheck_result(response, "<log>.log")

        self.assertIn("&lt;log&gt;.log", result)
        self.assertIn("&lt;b&gt;not markup&lt;/b&gt;", result)
        self.assertNotIn("<b>not markup</b>", result)

    def test_builds_compact_rich_message_with_release_cards(self):
        candidates = parse_release_candidates(
            MUSICBRAINZ_RESPONSE, LOGCHECK_RESPONSE["musicbrainz_id"]
        )
        rich_message = build_rich_logcheck_message(
            LOGCHECK_RESPONSE, "Hats.log", candidates
        )
        html = rich_message["html"]

        self.assertTrue(rich_message["skip_entity_detection"])
        self.assertIn("<h3>Rip log report</h3>", html)
        self.assertIn("<table compact><caption>Integrity</caption>", html)
        self.assertIn("<details><summary><b>Database checks</b>", html)
        self.assertIn("<details open><summary><b>1. RAYE", html)
        self.assertIn("View this release on MusicBrainz", html)
        self.assertIn("Exact Disc ID match.", html)
        self.assertNotIn("<i>None</i>", html)

    def test_rich_message_escapes_untrusted_text(self):
        response = dict(LOGCHECK_RESPONSE)
        response["details"] = ["<script>bad</script>"]
        rich_message = build_rich_logcheck_message(response, "<log>.log")

        self.assertIn("&lt;log&gt;.log", rich_message["html"])
        self.assertIn("&lt;script&gt;bad&lt;/script&gt;", rich_message["html"])
        self.assertNotIn("<script>bad</script>", rich_message["html"])

    def test_fetch_uses_toc_fallback_and_identifying_user_agent(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["request"] = request
            return httpx.Response(200, json=MUSICBRAINZ_RESPONSE)

        async def run():
            logchecker._last_musicbrainz_request = 0.0
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await fetch_release_candidates(client, LOGCHECK_RESPONSE)

        candidates = asyncio.run(run())
        request = seen["request"]
        query = parse_qs(request.url.query.decode())

        self.assertEqual(len(candidates), 1)
        self.assertEqual(query["toc"], ["1+17+331818+150+5651"])
        self.assertEqual(query["cdstubs"], ["no"])
        self.assertIn("spideyonmoon/alfred-audio-bot", request.headers["User-Agent"])

    def test_sends_rich_message_through_bot_api_with_reply_and_topic(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["request"] = request
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 101}})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await send_rich_logcheck_message(
                    client,
                    "test-token",
                    -100123,
                    44,
                    77,
                    {"html": "<h3>Report</h3>", "skip_entity_detection": True},
                )

        asyncio.run(run())
        request = seen["request"]
        payload = json.loads(request.content)

        self.assertEqual(request.url.path, "/bottest-token/sendRichMessage")
        self.assertEqual(payload["chat_id"], -100123)
        self.assertEqual(payload["message_thread_id"], 77)
        self.assertEqual(payload["reply_parameters"]["message_id"], 44)
        self.assertTrue(payload["reply_parameters"]["allow_sending_without_reply"])


if __name__ == "__main__":
    unittest.main()
