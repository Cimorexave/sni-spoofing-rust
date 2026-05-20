"""
Tests for auto-runner.bat logic (extracted into Python for verifiable, repeatable testing).

The batch script's core logic is:
  Scenario A (valid reachable IP):
    config.json has "connect": "104.18.6.147:443" (or similar)
    → IP extracted, validated as IPv4
    → Ping succeeds
    → Reverse-lookup: find config in cfgs.txt whose SNI domain resolves to that IP
    → Display found config, launch sni-spoof-rs.exe

  Scenario B (invalid / empty / unreachable IP):
    config.json has "connect": ":443" or "connect": "999.999.999.999:443"
    → IP fails IPv4 validation, or ping fails
    → Search cfgs.txt for a working SNI (DNS + TCP connect on 443)
    → Pick first working config, update config.json with its resolved IP
    → Display config, launch sni-spoof-rs.exe

This test suite reimplements those PowerShell commands in Python and uses mocks
so no real network I/O is required.
"""

import json
import os
import re
import socket
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers that mirror the PowerShell logic in auto-runner.bat
# ---------------------------------------------------------------------------

def extract_ip_from_connect(connect_value: str) -> Optional[str]:
    """
    Mirror of the PowerShell:
      $conn = $c.listeners[0].connect
      if ([string]::IsNullOrEmpty($conn)) { exit }
      $ip = ($conn -split ':')[0]
      Write-Output $ip
    """
    if not connect_value:
        return None
    return connect_value.split(":")[0]


def is_valid_ipv4(ip: str) -> bool:
    """
    Mirror of the PowerShell:
      $ok = [System.Net.IPAddress]::TryParse($ip, [ref]$parsed)
      if (-not $ok) { exit 1 }
      if ($parsed.AddressFamily -ne 'InterNetwork') { exit 1 }
      exit 0
    Returns True if ip is a valid IPv4 address.
    """
    try:
        addr = socket.inet_pton(socket.AF_INET, ip)
        return True
    except (OSError, socket.error):
        return False


def extract_sni_from_config(config_line: str) -> Optional[str]:
    """
    Mirror of the PowerShell used in STEP 2 (reverse-lookup) and STEP 3 (search):
      if ($l -match '^trojan://') {
        $p = [regex]::Match($l, 'sni=([^&]+)')
        $domain = if ($p.Success) { $p.Groups[1].Value } else { $null }
      } elseif ($l -match '^vless://') {
        $p = [regex]::Match($l, 'host=([^&]+)')
        $domain = if ($p.Success) { $p.Groups[1].Value } else { $null }
      } else { $domain = $null }
    """
    if config_line.startswith("trojan://"):
        m = re.search(r"sni=([^&]+)", config_line)
        return m.group(1) if m else None
    elif config_line.startswith("vless://"):
        m = re.search(r"host=([^&]+)", config_line)
        return m.group(1) if m else None
    return None


def resolve_ip(domain: str) -> list[str]:
    """
    Mirror of:
      $ips = [System.Net.Dns]::GetHostAddresses($domain)
    Returns list of IP strings.
    """
    try:
        addrs = socket.getaddrinfo(domain, 443, socket.AF_INET)
        return list(dict.fromkeys(a[4][0] for a in addrs))
    except socket.gaierror:
        return []


def tcp_check(host: str, port: int = 443, timeout: float = 3.0) -> bool:
    """
    Mirror of the TCP connect test in STEP 3:
      $tcp = New-Object System.Net.Sockets.TcpClient
      $conn = $tcp.BeginConnect($domain, 443, $null, $null)
      $wait = $conn.AsyncWaitHandle.WaitOne(3000, $false)
      if ($wait -and $tcp.Connected) { ... }
    """
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except (OSError, socket.error):
        return False


# ---------------------------------------------------------------------------
# Scenario A logic: reverse-lookup a config whose SNI resolves to target_ip
# ---------------------------------------------------------------------------

def reverse_lookup_config(cfgs_lines: list[str], target_ip: str) -> Optional[str]:
    """
    Mirror of the STEP 2 reverse-lookup PowerShell.
    Iterates cfgs.txt lines; for each, extracts SNI domain, resolves it,
    and checks if any resolved IP matches target_ip.
    Returns the matching config line, or None.
    """
    for line in cfgs_lines:
        line = line.strip()
        if not line:
            continue
        domain = extract_sni_from_config(line)
        if not domain:
            continue
        try:
            ips = resolve_ip(domain)
        except Exception:
            ips = []
        for ip_str in ips:
            if ip_str == target_ip:
                return line
    return None


# ---------------------------------------------------------------------------
# Scenario B logic: search cfgs.txt for a working config (DNS + TCP)
# ---------------------------------------------------------------------------

def search_working_config(cfgs_lines: list[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Mirror of the STEP 3 search PowerShell.
    Iterates cfgs.txt lines; extracts SNI domain, resolves DNS,
    then attempts TCP connect on port 443.
    Returns (config_line, resolved_ip) of the first working config,
    or (None, None).
    """
    for line in cfgs_lines:
        line = line.strip()
        if not line:
            continue
        domain = extract_sni_from_config(line)
        if not domain:
            continue
        ips = resolve_ip(domain)
        if not ips:
            continue
        ip = ips[0]
        if tcp_check(domain, 443):
            return line, ip
    return None, None


# ---------------------------------------------------------------------------
# Config.json helpers
# ---------------------------------------------------------------------------

def read_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def update_config_connect(config_path: str, new_connect: str):
    cfg = read_config(config_path)
    cfg["listeners"][0]["connect"] = new_connect
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)


# ===========================================================================
# TESTS
# ===========================================================================

class TestIPExtraction(unittest.TestCase):
    """Tests for extracting IP from config.json's connect field."""

    def test_valid_ip_with_port(self):
        self.assertEqual(extract_ip_from_connect("104.18.6.147:443"), "104.18.6.147")

    def test_valid_ip_with_different_port(self):
        self.assertEqual(extract_ip_from_connect("172.67.213.5:8443"), "172.67.213.5")

    def test_empty_string(self):
        self.assertIsNone(extract_ip_from_connect(""))

    def test_none_value(self):
        self.assertIsNone(extract_ip_from_connect(None))

    def test_only_port(self):
        self.assertEqual(extract_ip_from_connect(":443"), "")

    def test_ipv6_not_relevant_but_handled(self):
        """IPv6 would match but the batch only cares about IPv4."""
        self.assertEqual(extract_ip_from_connect("::1:443"), "")


class TestIPv4Validation(unittest.TestCase):
    """Tests for the IPv4 validation logic (mirrors PowerShell TryParse)."""

    def test_valid_ipv4_104(self):
        self.assertTrue(is_valid_ipv4("104.18.6.147"))

    def test_valid_ipv4_172(self):
        self.assertTrue(is_valid_ipv4("172.67.213.5"))

    def test_valid_ipv4_localhost(self):
        self.assertTrue(is_valid_ipv4("127.0.0.1"))

    def test_valid_ipv4_private(self):
        self.assertTrue(is_valid_ipv4("192.168.1.1"))

    def test_empty_string(self):
        self.assertFalse(is_valid_ipv4(""))

    def test_invalid_octet_range(self):
        self.assertFalse(is_valid_ipv4("999.999.999.999"))

    def test_non_ip_string(self):
        self.assertFalse(is_valid_ipv4("not-an-ip"))

    def test_ipv6_rejected(self):
        self.assertFalse(is_valid_ipv4("2001:db8::1"))


class TestSNIExtraction(unittest.TestCase):
    """Tests for extracting SNI domain from trojan:// and vless:// config lines."""

    def test_trojan_sni_extracted(self):
        line = "trojan://humanity@127.0.0.1:40443?security=tls&sni=www.creationlong.org&insecure=0&allowInsecure=0&type=ws&host=www.creationlong.org&path=%2Fassignment#1"
        self.assertEqual(extract_sni_from_config(line), "www.creationlong.org")

    def test_trojan_sni_with_fingerprint(self):
        line = "trojan://humanity@127.0.0.1:40443?security=tls&sni=www.creationlong.org&fp=chrome&alpn=h2%2Chttp%2F1.1&insecure=0&allowInsecure=0&type=ws&host=www.creationlong.org&path=%2Fassignment#%F0%9F%8F%B3%EF%B8%8F%20ShatakVPN%20350884"
        self.assertEqual(extract_sni_from_config(line), "www.creationlong.org")

    def test_trojan_no_sni_returns_none(self):
        line = "trojan://humanity@127.0.0.1:40443?security=tls&insecure=1&allowInsecure=1&type=ws&host=www.gossipglove.com&path=%2Fassignment#%F0%9F%8F%B3%EF%B8%8F%20ShatakVPN%20143906"
        self.assertIsNone(extract_sni_from_config(line))

    def test_vless_sni_from_host(self):
        line = "vless://6202b230-417c-4d8e-b624-0f71afa9c75d@127.0.0.1:40443?encryption=none&security=tls&sni=sni.111000.indevs.in&insecure=1&allowInsecure=1&type=ws&host=sni.111000.indevs.in&path=%2F%3FTelegram%D9%8B%20%40ProxyVPN11#v2ray_dalghak"
        self.assertEqual(extract_sni_from_config(line), "sni.111000.indevs.in")

    def test_vless_host_extracted(self):
        """vless uses host= parameter, not sni=."""
        line = "vless://6202b230-417c-4d8e-b624-0f71afa9c75d@127.0.0.1:40443?encryption=none&security=tls&sni=sni.111000.indevs.in&insecure=0&allowInsecure=0&type=ws&host=sni.111000.indevs.in&path=%2F#%F0%9F%87%B1%F0%9F%87%BB%20LV%20%40ByGFW"
        self.assertEqual(extract_sni_from_config(line), "sni.111000.indevs.in")

    def test_invalid_line_returns_none(self):
        self.assertIsNone(extract_sni_from_config("this is not a config line"))

    def test_empty_line_returns_none(self):
        self.assertIsNone(extract_sni_from_config(""))


class TestReverseLookup(unittest.TestCase):
    """Scenario 1: Valid IPv4 found in config.json → reverse-lookup matching config."""

    def setUp(self):
        # Sample cfgs lines - a subset of the real cfgs.txt
        self.cfgs = [
            "trojan://humanity@127.0.0.1:40443?security=tls&sni=www.creationlong.org&insecure=0&allowInsecure=0&type=ws&host=www.creationlong.org&path=%2Fassignment#1",
            "trojan://humanity@127.0.0.1:40443?security=tls&sni=www.multiplydose.com&insecure=1&allowInsecure=1&type=ws&host=www.multiplydose.com&path=%2Fassignment#AmyraxVPN%40%205%20%E2%9A%A1",
            "trojan://humanity@127.0.0.1:40443?security=tls&sni=www.ignitelimit.com&insecure=1&allowInsecure=1&type=ws&host=www.ignitelimit.com&path=%2Fassignment#Technologia%20%3A%20Franch",
            "vless://6202b230-417c-4d8e-b624-0f71afa9c75d@127.0.0.1:40443?encryption=none&security=tls&sni=sni.111000.indevs.in&insecure=1&allowInsecure=1&type=ws&host=sni.111000.indevs.in&path=%2F%3FTelegram%D9%8B%20%40ProxyVPN11#v2ray_dalghak",
        ]

    @patch("socket.getaddrinfo")
    def test_reverse_lookup_finds_matching_config(self, mock_getaddrinfo):
        """
        Given config.json has connect="104.18.6.147:443",
        and www.creationlong.org resolves to 104.18.6.147,
        the reverse-lookup should find the trojan config with that SNI.
        """
        # Mock DNS: www.creationlong.org → 104.18.6.147
        def mock_resolve(host, port, family=0, *args, **kwargs):
            if host == "www.creationlong.org":
                return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("104.18.6.147", 0))]
            if host == "www.multiplydose.com":
                return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("172.67.213.5", 0))]
            raise socket.gaierror("Mock DNS failure")

        mock_getaddrinfo.side_effect = mock_resolve

        found = reverse_lookup_config(self.cfgs, "104.18.6.147")
        self.assertIsNotNone(found, "Should find a matching config")
        self.assertIn("www.creationlong.org", found)
        self.assertTrue(found.startswith("trojan://"))

    @patch("socket.getaddrinfo")
    def test_reverse_lookup_different_ip_finds_different_config(self, mock_getaddrinfo):
        """
        Given config.json has connect="172.67.213.5:443",
        should find the multiplydose config.
        """
        def mock_resolve(host, port, family=0, *args, **kwargs):
            if host == "www.creationlong.org":
                return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("104.18.6.147", 0))]
            if host == "www.multiplydose.com":
                return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("172.67.213.5", 0))]
            raise socket.gaierror("Mock DNS failure")

        mock_getaddrinfo.side_effect = mock_resolve

        found = reverse_lookup_config(self.cfgs, "172.67.213.5")
        self.assertIsNotNone(found, "Should find a matching config")
        self.assertIn("www.multiplydose.com", found)

    @patch("socket.getaddrinfo")
    def test_reverse_lookup_no_match_returns_none(self, mock_getaddrinfo):
        """Given an IP that no SNI resolves to, should return None."""
        def mock_resolve(host, port, family=0, *args, **kwargs):
            if host == "www.creationlong.org":
                return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("104.18.6.147", 0))]
            raise socket.gaierror("Mock DNS failure")

        mock_getaddrinfo.side_effect = mock_resolve

        found = reverse_lookup_config(self.cfgs, "10.0.0.99")
        self.assertIsNone(found)

    @patch("socket.getaddrinfo")
    def test_reverse_lookup_dns_failure_skips_config(self, mock_getaddrinfo):
        """If DNS fails for a domain, that config should be skipped."""
        mock_getaddrinfo.side_effect = socket.gaierror("DNS failure")

        found = reverse_lookup_config(self.cfgs, "104.18.6.147")
        self.assertIsNone(found)


class TestSearchWorkingConfig(unittest.TestCase):
    """Scenario 2: Invalid/empty IP → search cfgs.txt for a working config."""

    def setUp(self):
        self.cfgs = [
            "trojan://humanity@127.0.0.1:40443?security=tls&sni=www.creationlong.org&insecure=0&allowInsecure=0&type=ws&host=www.creationlong.org&path=%2Fassignment#1",
            "trojan://humanity@127.0.0.1:40443?security=tls&sni=www.multiplydose.com&insecure=1&allowInsecure=1&type=ws&host=www.multiplydose.com&path=%2Fassignment#AmyraxVPN%40%205%20%E2%9A%A1",
            "trojan://humanity@127.0.0.1:40443?security=tls&sni=www.ignitelimit.com&insecure=1&allowInsecure=1&type=ws&host=www.ignitelimit.com&path=%2Fassignment#Technologia%20%3A%20Franch",
        ]

    @patch("socket.getaddrinfo")
    @patch("socket.create_connection")
    def test_search_finds_first_working_config(self, mock_create_conn, mock_getaddrinfo):
        """
        When the IP is invalid, STEP 3 searches cfgs.txt.
        It should find the first config whose SNI resolves DNS and
        passes the TCP check on port 443.
        """
        # First domain resolves OK, TCP passes
        def mock_resolve(host, port, family=0, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("104.18.6.147", 0))]

        mock_getaddrinfo.side_effect = mock_resolve
        mock_create_conn.return_value.__enter__.return_value = MagicMock()

        config_line, resolved_ip = search_working_config(self.cfgs)
        self.assertIsNotNone(config_line)
        self.assertIn("www.creationlong.org", config_line)
        self.assertEqual(resolved_ip, "104.18.6.147")

    @patch("socket.getaddrinfo")
    @patch("socket.create_connection")
    def test_search_skips_dns_failures(self, mock_create_conn, mock_getaddrinfo):
        """
        If first domain fails DNS, should skip to next.
        If second domain resolves and TCP passes, should return that.
        """
        resolve_results = [
            socket.gaierror("DNS failure"),          # first fails
            [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("172.67.213.5", 0))],  # second succeeds
        ]
        mock_getaddrinfo.side_effect = resolve_results
        mock_create_conn.return_value.__enter__.return_value = MagicMock()

        config_line, resolved_ip = search_working_config(self.cfgs)
        self.assertIsNotNone(config_line)
        self.assertIn("www.multiplydose.com", config_line)
        self.assertEqual(resolved_ip, "172.67.213.5")

    @patch("socket.getaddrinfo")
    @patch("socket.create_connection")
    def test_search_skips_tcp_failures(self, mock_create_conn, mock_getaddrinfo):
        """
        If DNS resolves but TCP fails, should skip to next config.
        """
        def mock_resolve(host, port, family=0, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("104.18.6.147", 0))]

        mock_getaddrinfo.side_effect = mock_resolve
        # First TCP fails, second succeeds
        create_conn_results = [
            OSError("Connection refused"),  # first fails
            MagicMock(),                     # second succeeds
        ]
        mock_create_conn.side_effect = create_conn_results

        # Only first two configs (first TCP fails, second succeeds)
        config_line, resolved_ip = search_working_config(self.cfgs[:2])
        self.assertIsNotNone(config_line)
        self.assertIn("www.multiplydose.com", config_line)

    @patch("socket.getaddrinfo")
    @patch("socket.create_connection")
    def test_search_all_fail_returns_none(self, mock_create_conn, mock_getaddrinfo):
        """If all configs fail DNS or TCP, should return (None, None)."""
        mock_getaddrinfo.side_effect = socket.gaierror("DNS failure")

        config_line, resolved_ip = search_working_config(self.cfgs)
        self.assertIsNone(config_line)
        self.assertIsNone(resolved_ip)

    @patch("socket.getaddrinfo")
    @patch("socket.create_connection")
    def test_search_skips_configs_without_sni(self, mock_create_conn, mock_getaddrinfo):
        """
        Configs without an SNI parameter (or without host= for vless)
        should be skipped, even if they appear first.
        """
        cfgs_without_sni = [
            "trojan://humanity@127.0.0.1:40443?security=tls&insecure=1&allowInsecure=1&type=ws&host=www.gossipglove.com&path=%2Fassignment#skip_me",
            "trojan://humanity@127.0.0.1:40443?security=tls&sni=www.creationlong.org&insecure=0&allowInsecure=0&type=ws&host=www.creationlong.org&path=%2Fassignment#1",
        ]

        def mock_resolve(host, port, family=0, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("104.18.6.147", 0))]

        mock_getaddrinfo.side_effect = mock_resolve
        mock_create_conn.return_value.__enter__.return_value = MagicMock()

        config_line, resolved_ip = search_working_config(cfgs_without_sni)
        self.assertIsNotNone(config_line)
        # Should have found the second config (with SNI)
        self.assertIn("www.creationlong.org", config_line)
        # Should NOT have returned the first one (no SNI)
        self.assertNotIn("skip_me", config_line)


class TestConfigFileIntegration(unittest.TestCase):
    """Integration tests: reading/writing config.json like the batch does."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmpdir, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({
                "listeners": [
                    {
                        "listen": "0.0.0.0:40443",
                        "connect": ":443",
                        "fake_sni": "security.vercel.com"
                    }
                ]
            }, f, indent=4)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_config_extracts_connect(self):
        """Verify config.json can be read and connect field extracted."""
        cfg = read_config(self.config_path)
        connect = cfg["listeners"][0]["connect"]
        ip = extract_ip_from_connect(connect)
        self.assertEqual(ip, "")  # currently ":443" → splits to ""

    def test_update_config_with_valid_ip(self):
        """Simulate STEP 3c: update config.json with a new IP."""
        new_connect = "104.18.6.147:443"
        update_config_connect(self.config_path, new_connect)

        cfg = read_config(self.config_path)
        self.assertEqual(cfg["listeners"][0]["connect"], "104.18.6.147:443")

        ip = extract_ip_from_connect(cfg["listeners"][0]["connect"])
        self.assertEqual(ip, "104.18.6.147")
        self.assertTrue(is_valid_ipv4(ip))

    def test_update_config_with_another_ip(self):
        """Verify updating with the second known working IP."""
        new_connect = "172.67.213.5:443"
        update_config_connect(self.config_path, new_connect)

        cfg = read_config(self.config_path)
        self.assertEqual(cfg["listeners"][0]["connect"], "172.67.213.5:443")
        ip = extract_ip_from_connect(cfg["listeners"][0]["connect"])
        self.assertTrue(is_valid_ipv4(ip))

    def test_config_json_reads_with_bom(self):
        """
        config.json has a UTF-8 BOM (as seen in the real file).
        Verify Python's utf-8-sig handles it.
        """
        # Write with BOM (utf-8-sig encoding adds the BOM automatically)
        with open(self.config_path, "w", encoding="utf-8-sig") as f:
            f.write('{"listeners":[{"connect":"104.18.6.147:443"}]}')
        cfg = read_config(self.config_path)
        self.assertEqual(cfg["listeners"][0]["connect"], "104.18.6.147:443")


class TestScenarioFullFlow(unittest.TestCase):
    """
    End-to-end scenario tests that simulate the batch file's full decision flow
    without executing the batch file or requiring real network access.
    """

    @patch("socket.getaddrinfo")
    @patch("socket.create_connection")
    def test_scenario1_valid_ip_found_in_config(self, mock_create_conn, mock_getaddrinfo):
        """
        SCENARIO 1: config.json has a valid reachable IP (e.g. 104.18.6.147).
        Expected flow:
          1. IP extracted and validated ✓
          2. Ping succeeds (simulated - not tested here, but IP is valid)
          3. Reverse-lookup finds matching config in cfgs.txt ✓
          4. Config displayed to user ✓
          5. sni-spoof-rs.exe launched (not tested - external exe)
        """
        # Arrange: simulate config.json with valid IP
        connect_value = "104.18.6.147:443"
        ip = extract_ip_from_connect(connect_value)
        self.assertEqual(ip, "104.18.6.147")
        self.assertTrue(is_valid_ipv4(ip), "IP must be a valid IPv4")

        # Arrange: mock DNS so that www.creationlong.org resolves to 104.18.6.147
        def mock_resolve(host, port, family=0, *args, **kwargs):
            mapping = {
                "www.creationlong.org": "104.18.6.147",
                "www.multiplydose.com": "172.67.213.5",
            }
            if host in mapping:
                return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (mapping[host], 0))]
            raise socket.gaierror("Mock DNS failure")
        mock_getaddrinfo.side_effect = mock_resolve

        # Act: reverse-lookup in cfgs.txt
        cfgs = [
            "trojan://humanity@127.0.0.1:40443?security=tls&sni=www.creationlong.org&insecure=0&allowInsecure=0&type=ws&host=www.creationlong.org&path=%2Fassignment#1",
            "trojan://humanity@127.0.0.1:40443?security=tls&sni=www.multiplydose.com&insecure=1&allowInsecure=1&type=ws&host=www.multiplydose.com&path=%2Fassignment#AmyraxVPN%40%205%20%E2%9A%A1",
        ]
        found_config = reverse_lookup_config(cfgs, ip)

        # Assert: the correct config is found
        self.assertIsNotNone(found_config, "Must find a config for the valid IP")
        self.assertIn("www.creationlong.org", found_config,
                      "Should match the config whose SNI resolves to 104.18.6.147")

        # Verify the config starts with the expected protocol
        self.assertTrue(found_config.startswith("trojan://"),
                        "Config should be a valid trojan:// URI")

        # The found config would be written to sni_config.tmp and displayed
        # In the real batch: popup shows this config, then launches sni-spoof-rs.exe
        # Here we just verify the config content is correct
        sni_domain = extract_sni_from_config(found_config)
        self.assertEqual(sni_domain, "www.creationlong.org")

    @patch("socket.getaddrinfo")
    @patch("socket.create_connection")
    def test_scenario2_invalid_ip_searches_and_finds_working(
        self, mock_create_conn, mock_getaddrinfo
    ):
        """
        SCENARIO 2: config.json has an invalid/empty IP.
        Cases: empty string, invalid IPv4, non-reachable IP.
        Expected flow:
          1. IP extraction yields "" or invalid
          2. IPv4 validation fails → skip ping
          3. Search cfgs.txt for working SNI (DNS + TCP)
          4. First working config found, IP extracted
          5. config.json updated with new IP
          6. Config displayed, sni-spoof-rs.exe launched
        """
        # --- Sub-case A: Empty connect value ---
        connect_value = ":443"
        ip = extract_ip_from_connect(connect_value)
        self.assertEqual(ip, "", "Empty connect should yield empty IP")
        self.assertFalse(is_valid_ipv4(ip), "Empty IP is not valid IPv4")

        # --- Sub-case B: Invalid IP string ---
        connect_value = "not-an-ip:443"
        ip = extract_ip_from_connect(connect_value)
        self.assertEqual(ip, "not-an-ip")
        self.assertFalse(is_valid_ipv4(ip), "Garbage string is not valid IPv4")

        # --- Sub-case C: Out-of-range octet ---
        connect_value = "999.999.999.999:443"
        ip = extract_ip_from_connect(connect_value)
        self.assertEqual(ip, "999.999.999.999")
        self.assertFalse(is_valid_ipv4(ip), "Out-of-range IP is not valid IPv4")

        # --- Now test the search flow (Scenario B main path) ---
        # Mock DNS + TCP to simulate finding a working config
        def mock_resolve(host, port, family=0, *args, **kwargs):
            mapping = {
                "www.ignitelimit.com": "104.18.6.147",
                "www.multiplydose.com": "172.67.213.5",
            }
            if host in mapping:
                return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (mapping[host], 0))]
            raise socket.gaierror("Mock DNS failure")
        mock_getaddrinfo.side_effect = mock_resolve
        mock_create_conn.return_value.__enter__.return_value = MagicMock()

        cfgs = [
            "trojan://humanity@127.0.0.1:40443?security=tls&sni=www.ignitelimit.com&insecure=1&allowInsecure=1&type=ws&host=www.ignitelimit.com&path=%2Fassignment#Technologia%20%3A%20Franch",
            "trojan://humanity@127.0.0.1:40443?security=tls&sni=www.multiplydose.com&insecure=1&allowInsecure=1&type=ws&host=www.multiplydose.com&path=%2Fassignment#AmyraxVPN%40%205%20%E2%9A%A1",
        ]

        config_line, resolved_ip = search_working_config(cfgs)

        self.assertIsNotNone(config_line, "Should find a working config")
        self.assertIsNotNone(resolved_ip, "Should resolve an IP")
        self.assertIn("www.ignitelimit.com", config_line,
                      "Should pick the first working config")
        self.assertEqual(resolved_ip, "104.18.6.147")

        # In the real batch, this IP would be written back to config.json:
        # $config.listeners[0].connect = '104.18.6.147:443'
        new_connect = f"{resolved_ip}:443"
        self.assertEqual(new_connect, "104.18.6.147:443")

    @patch("socket.getaddrinfo")
    @patch("socket.create_connection")
    def test_scenario2_empty_cfgs_no_working_found(
        self, mock_create_conn, mock_getaddrinfo
    ):
        """
        Edge case: config.json has invalid IP, BUT cfgs.txt is empty
        or all configs fail. Should return (None, None) and the batch
        would show "ERROR: No working SNI configuration found" and exit.
        """
        mock_getaddrinfo.side_effect = socket.gaierror("DNS failure")

        config_line, resolved_ip = search_working_config([])
        self.assertIsNone(config_line)
        self.assertIsNone(resolved_ip)


# ---------------------------------------------------------------------------
# cfgs.txt data integrity check (against the actual file in runner/)
# ---------------------------------------------------------------------------

class TestRealCfgsFile(unittest.TestCase):
    """Validates the actual cfgs.txt file structure and extractability."""

    @classmethod
    def setUpClass(cls):
        cfgs_path = os.path.join(os.path.dirname(__file__), "cfgs.txt")
        if os.path.exists(cfgs_path):
            with open(cfgs_path, "r", encoding="utf-8") as f:
                cls.lines = [l.strip() for l in f if l.strip()]
        else:
            cls.lines = []

    def test_all_lines_are_parseable(self):
        """Every non-empty line in cfgs.txt should be parseable by the batch."""
        for i, line in enumerate(self.lines, 1):
            sni = extract_sni_from_config(line)
            if line.startswith("trojan://"):
                # trojan configs SHOULD have sni= parameter (but some don't)
                if sni is None:
                    # Line 18: trojan without sni=, this is expected to be skipped
                    pass
            elif line.startswith("vless://"):
                self.assertIsNotNone(
                    sni,
                    f"Line {i}: vless config must have host= parameter: {line[:60]}..."
                )

    def test_no_unknown_protocols(self):
        """All lines should start with trojan:// or vless://."""
        for i, line in enumerate(self.lines, 1):
            if not (line.startswith("trojan://") or line.startswith("vless://")):
                self.fail(f"Line {i}: unknown protocol: {line[:60]}...")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
