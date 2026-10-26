import lzma
import os
import struct
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from legendclient import flash, game, servers

PORTAL_HTML = """
<div class="server_played">
  <a href="/serverlist/s3265" class="recomm_l1"><span></span>
     OAS1265:C&atilde;o de Tr&ecirc;s Olhos <i></i></a>
</div>
<a href="javascript:void(0)" data-url="/serverlist/s9999" id="s_quick_play">Jogar</a>
<a href="/serverlist/s9999">Jogar</a>
<ul>
  <li><a href="/serverlist/s3265"><span></span> OAS1265:Cão de Três Olhos </a></li>
  <li><a href="/serverlist/s3264"><span></span> OAS1264:Gole de Cervejinha</a></li>
  <li><a href="/serverlist/s12">Servidor sem código</a></li>
  <li><a href="/serverlist/s13"><span></span></a></li>
  <li><a href="/articlelist/news">Notícias</a></li>
</ul>
"""


class ServerListTest(unittest.TestCase):
    def test_parse_dedupes_sorts_and_skips_placeholders(self):
        found = servers.parse_servers(PORTAL_HTML)
        self.assertEqual([s.id for s in found], [3265, 3264, 12])
        self.assertEqual(found[0], servers.Server(3265, "OAS1265", "Cão de Três Olhos"))
        self.assertEqual(found[2].label, "Servidor sem código")
        self.assertEqual(found[1].label, "OAS1264 — Gole de Cervejinha")

    def test_urls(self):
        self.assertEqual(servers.play_url("lobr", 3265),
                         "https://lobr.creaction-network.com/serverlist/s3265")
        with self.assertRaises(ValueError):
            servers.portal_url("evil.example/")


GAME_FRAME = ("https://s1217oas.creaction-network.com/client/game.jsp"
              "?user=359&key=15d4e891d784977cacbfcbb00c48f133&site=broas_1237")


class GameStartTest(unittest.TestCase):
    def test_builds_loading_swf_call_like_game_jsp(self):
        start = game.start_from_game_frame(GAME_FRAME)
        self.assertRegex(start.movie,
                         r"^https://s1217oas\.creaction-network\.com/client/Loading\.swf\?rnd=\d+$")
        self.assertEqual(start.flashvars,
                         "user=359&key=15d4e891d784977cacbfcbb00c48f133&site=broas_1237"
                         "&p2=&rid=&logintype=&http=https%3A")
        self.assertEqual(start.params["base"], "https://s1217oas.creaction-network.com/client/")
        self.assertEqual(start.params["wmode"], "direct")

    def test_rejects_unexpected_or_incomplete_frames(self):
        for url in ("https://evil.example/client/other.jsp?user=1&key=2",
                    "file:///client/game.jsp?user=1&key=2",
                    "https://s1.creaction-network.com/client/game.jsp?user=1"):
            with self.assertRaises(game.GameError, msg=url):
                game.start_from_game_frame(url)


class HostConfigTest(unittest.TestCase):
    def start(self, **kw):
        data = {"movie": "https://s1.example/Loading.swf", "flashvars": "user=1&key=abc",
                "params": {"wmode": "opaque", "bgcolor": "#000000", "quality": "high",
                           "unknown": "x"}}
        data.update(kw)
        return game.GameStart(**data)

    def test_config_maps_params_and_puts_movie_last(self):
        text = game.host_config(self.start(), Path("/opt/Flash.ocx"), "Jogo")
        lines = text.splitlines()
        self.assertEqual(lines[0], "ocx=Z:\\opt\\Flash.ocx")
        self.assertIn("prop.WMode=opaque", lines)
        self.assertIn("prop.BGColor=#000000", lines)
        self.assertIn("prop.Quality2=high", lines)
        self.assertIn("prop.AllowScriptAccess=always", lines)
        self.assertFalse(any("unknown" in line for line in lines))
        self.assertEqual(lines[-2:], ["flashvars=user=1&key=abc",
                                      "movie=https://s1.example/Loading.swf"])

    def test_cache_dir_is_passed_as_unix_path(self):
        text = game.host_config(self.start(), Path("/opt/Flash.ocx"), "Jogo",
                                cache_dir=Path("/tmp/cache"))
        self.assertIn("cache=/tmp/cache", text.splitlines())

    def test_rejects_line_injection(self):
        with self.assertRaises(game.GameError):
            game.host_config(self.start(flashvars="a=1\nocx=C:\\evil.ocx"),
                             Path("/opt/Flash.ocx"), "Jogo")


class FontCheckTest(unittest.TestCase):
    def test_reports_fonts_missing_from_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            self.assertEqual(game.missing_fonts(prefix), ["arial.ttf", "verdana.ttf"])
            fonts = prefix / "drive_c/windows/Fonts"
            fonts.mkdir(parents=True)
            (fonts / "Arial.TTF").touch()
            self.assertEqual(game.missing_fonts(prefix), ["verdana.ttf"])


class FindOcxTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.prefix = self.root / "prefix"
        self.bundled = self.root / "Flash64.ocx"
        patcher = mock.patch.object(flash, "OCX_PATH", self.bundled)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("LEGENDCLIENT_FLASH_OCX", None)

    def test_reports_how_to_install_when_missing(self):
        with self.assertRaisesRegex(game.GameError, "legendclient.flash"):
            game.find_ocx(self.prefix)

    def test_prefers_project_ocx_over_prefix(self):
        legacy = self.prefix / "drive_c/Program Files/App/flash/Flash64_1.ocx"
        legacy.parent.mkdir(parents=True)
        legacy.touch()
        self.assertEqual(game.find_ocx(self.prefix), legacy)
        self.bundled.touch()
        self.assertEqual(game.find_ocx(self.prefix), self.bundled)

    def test_environment_overrides_everything(self):
        self.bundled.touch()
        custom = self.root / "custom.ocx"
        custom.touch()
        os.environ["LEGENDCLIENT_FLASH_OCX"] = str(custom)
        self.assertEqual(game.find_ocx(self.prefix), custom)


class FlashUnpackTest(unittest.TestCase):
    def test_unpacks_installer_resource(self):
        payload = b"MZ" + bytes(range(256)) * 64
        raw = lzma.compress(payload, format=lzma.FORMAT_RAW, filters=[
            {"id": lzma.FILTER_LZMA1, "dict_size": 1 << 16, "lc": 3, "lp": 0, "pb": 2}])
        blob = b"\x02\x01\x00\x00" + bytes(7) + struct.pack("<BI", 0x5D, 1 << 16) + raw
        self.assertEqual(flash.unpack(blob)[:len(payload)], payload)

    def test_rejects_corrupt_resource(self):
        blob = bytes(11) + struct.pack("<BI", 0x5D, 1 << 16) + b"\xff" * 32
        with self.assertRaises(flash.FlashError):
            flash.unpack(blob)


class TrimCacheTest(unittest.TestCase):
    def test_removes_oldest_files_until_under_limit(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for age, name in enumerate(["novo", "medio", "velho"]):
                path = directory / name
                path.write_bytes(b"x" * 100)
                os.utime(path, (1000 - age, 1000 - age))
            game.trim_cache(directory, max_bytes=200)
            self.assertEqual(sorted(p.name for p in directory.iterdir()), ["medio", "novo"])


if __name__ == "__main__":
    unittest.main()
