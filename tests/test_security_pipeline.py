"""Unit tests for the ``security_pipeline`` helper functions."""

from __future__ import annotations

import argparse
import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

import security_pipeline as sp


@contextlib.contextmanager
def temporary_os_release(contents: str) -> Path:
    """Temporarily override ``KALI_OS_RELEASE_PATH`` with custom contents."""

    fd, filename = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(contents)
        original_path = sp.KALI_OS_RELEASE_PATH
        sp.KALI_OS_RELEASE_PATH = Path(filename)
        try:
            yield sp.KALI_OS_RELEASE_PATH
        finally:
            sp.KALI_OS_RELEASE_PATH = original_path
    finally:
        Path(filename).unlink(missing_ok=True)


class SecurityPipelineHelpersTest(unittest.TestCase):
    """Unit tests covering the Kali-specific helper logic."""

    def test_is_kali_linux_detects_keyword(self) -> None:
        with temporary_os_release('NAME="Kali"\nID=kali'):
            self.assertTrue(sp.is_kali_linux())

    def test_is_kali_linux_returns_false_for_other_distro(self) -> None:
        with temporary_os_release('NAME="Ubuntu"\nID=ubuntu'):
            self.assertFalse(sp.is_kali_linux())

    def test_build_kali_exploit_command_with_ports(self) -> None:
        with mock.patch.object(sp.shutil, "which", return_value="/usr/bin/nmap"):
            command = sp.build_kali_exploit_command("example.com", "80")
        self.assertEqual(command, "nmap --script vuln -p 80 example.com")

    def test_build_kali_exploit_command_without_nmap(self) -> None:
        with mock.patch.object(sp.shutil, "which", return_value=None):
            self.assertIsNone(sp.build_kali_exploit_command("example.com", "80"))

    def test_build_kali_post_command_prefers_enum4linux(self) -> None:
        def fake_which(name: str) -> Optional[str]:
            return "path" if name == "enum4linux" else None

        with mock.patch.object(sp.shutil, "which", side_effect=fake_which):
            self.assertEqual(sp.build_kali_post_command("example.com"), "enum4linux -a example.com")

    def test_build_kali_post_command_falls_back_to_nikto(self) -> None:
        def fake_which(name: str) -> Optional[str]:
            return "path" if name == "nikto" else None

        with mock.patch.object(sp.shutil, "which", side_effect=fake_which):
            self.assertEqual(sp.build_kali_post_command("example.com"), "nikto -host example.com")

    def test_apply_kali_defaults_populates_missing_commands(self) -> None:
        args = argparse.Namespace(
            target="example.com",
            ports="80",
            exploit_command=None,
            post_command=None,
            kali_defaults=True,
        )

        with mock.patch.object(sp, "is_kali_linux", return_value=True), mock.patch.object(
            sp, "build_kali_exploit_command", return_value="exploit"
        ), mock.patch.object(sp, "build_kali_post_command", return_value="post"):
            sp.apply_kali_defaults(args)

        self.assertEqual(args.exploit_command, "exploit")
        self.assertEqual(args.post_command, "post")

    def test_apply_kali_defaults_respects_existing_commands(self) -> None:
        args = argparse.Namespace(
            target="example.com",
            ports="80",
            exploit_command="custom-exploit",
            post_command="custom-post",
            kali_defaults=True,
        )

        with mock.patch.object(sp, "is_kali_linux", return_value=True), mock.patch.object(
            sp, "build_kali_exploit_command", return_value="exploit"
        ), mock.patch.object(sp, "build_kali_post_command", return_value="post"):
            sp.apply_kali_defaults(args)

        self.assertEqual(args.exploit_command, "custom-exploit")
        self.assertEqual(args.post_command, "custom-post")


if __name__ == "__main__":
    unittest.main()

