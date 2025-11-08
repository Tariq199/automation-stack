#!/usr/bin/env python3
"""Security automation CLI for orchestrating multi-stage assessments.

This module provides a command line entry point that can execute several
penetration testing related stages such as reconnaissance, network scanning,
exploitation checks, and post-exploitation tasks.  It is intentionally
extensible – power users can provide custom shell commands for the exploit and
post-exploitation phases, while the reconnaissance and scanning phases offer
useful defaults out of the box.

Example usage:

    python security_pipeline.py -t example.com -recon --scan
    python security_pipeline.py -t 192.168.1.10 --mobile-scan --nmap-args "-Pn"
    python security_pipeline.py -t target --full-scan --output results.json
    python security_pipeline.py -t target --full-scan --kali-defaults

The CLI options mirror the Arabic descriptions supplied in the original issue
request for easier adoption by native speakers.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence


@dataclass
class StageResult:
    """Container describing the outcome of a single stage."""

    name: str
    success: bool
    message: str
    command: Optional[List[str]] = None
    output: Optional[str] = None
    error: Optional[str] = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "success": self.success,
            "message": self.message,
            "command": self.command,
            "output": self.output,
            "error": self.error,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
        }


class StageExecutionError(RuntimeError):
    """Raised when a stage fails to execute due to invalid input."""


KALI_OS_RELEASE_PATH = Path("/etc/os-release")


def is_kali_linux() -> bool:
    """Best-effort detection to confirm whether the host is Kali Linux."""

    release_path = KALI_OS_RELEASE_PATH
    try:
        contents = release_path.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        return False

    lower_contents = contents.lower()
    if "kali" in lower_contents:
        return True

    for line in lower_contents.splitlines():
        if line.startswith("id=") and "kali" in line:
            return True
        if line.startswith("id_like=") and "kali" in line:
            return True

    # Fall back to checking environment variables that Kali typically sets.
    return "kali" in os.environ.get("DESKTOP_SESSION", "").lower()


def build_kali_exploit_command(target: Optional[str], ports: Optional[str]) -> Optional[str]:
    """Return a Kali-friendly exploit stage command when none is provided."""

    if not target or not shutil.which("nmap"):
        return None

    command: List[str] = ["nmap", "--script", "vuln"]
    if ports:
        command.extend(["-p", ports])
    command.append(target)
    return " ".join(shlex.quote(part) for part in command)


def build_kali_post_command(target: Optional[str]) -> Optional[str]:
    """Return a Kali-friendly post-exploitation command when available."""

    if not target:
        return None

    if shutil.which("enum4linux"):
        return f"enum4linux -a {shlex.quote(target)}"
    if shutil.which("nikto"):
        return f"nikto -host {shlex.quote(target)}"
    return None


def apply_kali_defaults(args: argparse.Namespace) -> None:
    """Populate exploit/post commands with Kali defaults when requested."""

    if not getattr(args, "kali_defaults", False):
        return

    if not is_kali_linux():
        print(
            "تحذير: لم يتم اكتشاف نظام Kali Linux، سيتم متابعة التنفيذ بالاعتماد على الأدوات المتوفرة.",
            file=sys.stderr,
        )

    if not args.target:
        return

    if not args.exploit_command:
        default_exploit = build_kali_exploit_command(args.target, args.ports)
        if default_exploit:
            args.exploit_command = default_exploit
        else:
            print(
                "تنبيه: لم يتم العثور على أمر افتراضي مناسب لمرحلة الاستغلال.",
                file=sys.stderr,
            )

    if not args.post_command:
        default_post = build_kali_post_command(args.target)
        if default_post:
            args.post_command = default_post
        else:
            print(
                "تنبيه: لم يتم العثور على أمر افتراضي مناسب لمرحلة ما بعد الاستغلال.",
                file=sys.stderr,
            )


class SecurityPipeline:
    """Coordinator that executes the requested assessment stages."""

    def __init__(
        self,
        target: Optional[str],
        ports: Optional[str],
        nmap_binary: str,
        nmap_args: Sequence[str],
        exploit_command: Optional[str],
        post_command: Optional[str],
        timeout: Optional[int],
    ) -> None:
        self.target = target
        self.ports = ports
        self.nmap_binary = nmap_binary
        self.nmap_args = list(nmap_args)
        self.exploit_command = exploit_command
        self.post_command = post_command
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Reconnaissance
    def run_recon(self) -> StageResult:
        start = datetime.utcnow()
        if not self.target:
            raise StageExecutionError("Reconnaissance requires a target (use --target/-t).")

        findings = {}
        try:
            resolved_ip = socket.gethostbyname(self.target)
            findings["resolved_ip"] = resolved_ip
        except socket.gaierror as exc:
            findings["resolved_ip_error"] = str(exc)

        try:
            addrinfo = socket.getaddrinfo(self.target, None)
            unique_ips = sorted({info[4][0] for info in addrinfo if info[4]})
            if unique_ips:
                findings["additional_ips"] = unique_ips
        except socket.gaierror:
            pass

        try:
            hostname, aliases, ips = socket.gethostbyaddr(findings.get("resolved_ip", self.target))
            findings["reverse_dns"] = {
                "hostname": hostname,
                "aliases": aliases,
                "ip_addresses": ips,
            }
        except (socket.herror, socket.gaierror):
            # Reverse lookup might legitimately fail; capture nothing.
            pass

        has_data = bool(findings)
        payload = dict(findings)
        payload["timestamp"] = datetime.utcnow().isoformat()
        message = "Reconnaissance complete." if has_data else "No reconnaissance data collected."
        return StageResult(
            name="recon",
            success=has_data,
            message=message,
            output=json.dumps(payload, indent=2, ensure_ascii=False),
            started_at=start,
            finished_at=datetime.utcnow(),
        )

    # ------------------------------------------------------------------
    # Nmap-based scans
    def run_network_scan(self, stage_name: str, extra_args: Iterable[str]) -> StageResult:
        start = datetime.utcnow()
        if not self.target:
            raise StageExecutionError("Network scanning requires a target (use --target/-t).")

        nmap_path = shutil.which(self.nmap_binary) or shutil.which("nmap")
        if not nmap_path:
            return StageResult(
                name=stage_name,
                success=False,
                message="Nmap is not installed or not found in PATH.",
                error="Missing nmap binary.",
                started_at=start,
                finished_at=datetime.utcnow(),
            )

        command: List[str] = [nmap_path]
        if self.ports:
            command += ["-p", self.ports]
        command.extend(extra_args)
        command.extend(self.nmap_args)
        command.append(self.target)

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return StageResult(
                name=stage_name,
                success=False,
                message="Nmap scan timed out.",
                command=command,
                error=str(exc),
                started_at=start,
                finished_at=datetime.utcnow(),
            )

        success = completed.returncode == 0
        message = "Scan completed successfully." if success else "Scan encountered issues."
        output = (completed.stdout or "").strip() or None
        error_output = (completed.stderr or "").strip() or None

        return StageResult(
            name=stage_name,
            success=success,
            message=message,
            command=command,
            output=output,
            error=error_output,
            started_at=start,
            finished_at=datetime.utcnow(),
        )

    def run_standard_scan(self) -> StageResult:
        return self.run_network_scan("scan", ["-sV"])

    def run_mobile_scan(self) -> StageResult:
        mobile_args = ["-sV", "-O", "--osscan-limit"]
        return self.run_network_scan("mobile-scan", mobile_args)

    # ------------------------------------------------------------------
    # Custom command stages
    def run_custom_stage(self, stage_name: str, command_str: Optional[str]) -> StageResult:
        start = datetime.utcnow()
        if not command_str:
            return StageResult(
                name=stage_name,
                success=True,
                message=f"No command provided for {stage_name} stage; skipping.",
                started_at=start,
                finished_at=datetime.utcnow(),
            )

        command_list = shlex.split(command_str)
        try:
            completed = subprocess.run(
                command_list,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            return StageResult(
                name=stage_name,
                success=False,
                message="Command not found.",
                command=command_list,
                error=str(exc),
                started_at=start,
                finished_at=datetime.utcnow(),
            )
        except subprocess.TimeoutExpired as exc:
            return StageResult(
                name=stage_name,
                success=False,
                message="Stage command timed out.",
                command=command_list,
                error=str(exc),
                started_at=start,
                finished_at=datetime.utcnow(),
            )

        success = completed.returncode == 0
        message = "Stage completed successfully." if success else "Stage finished with errors."
        output = (completed.stdout or "").strip() or None
        error_output = (completed.stderr or "").strip() or None

        return StageResult(
            name=stage_name,
            success=success,
            message=message,
            command=command_list,
            output=output,
            error=error_output,
            started_at=start,
            finished_at=datetime.utcnow(),
        )

    def run_exploit(self) -> StageResult:
        return self.run_custom_stage("exploit", self.exploit_command)

    def run_post_exploitation(self) -> StageResult:
        return self.run_custom_stage("post", self.post_command)


# ----------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="أداة لإدارة مراحل التقييم الأمني بشكل آلي.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-t",
        "--target",
        help="الهدف أو عنوان الـ IP المراد اختباره.",
    )
    parser.add_argument(
        "-p",
        "--ports",
        help="نطاق المنافذ المراد فحصها (مثال 1-1024 أو 80,443).",
    )
    parser.add_argument(
        "--nmap-binary",
        default="nmap",
        help="المسار التنفيذي لأداة Nmap في حال لم تكن ضمن PATH.",
    )
    parser.add_argument(
        "--nmap-args",
        default="",
        help="خيارات إضافية تمرر إلى Nmap (يتم تحليلها باستخدام shlex).",
    )
    parser.add_argument(
        "--exploit-command",
        help="أمر مخصص لتنفيذ مرحلة الاستغلال.",
    )
    parser.add_argument(
        "--post-command",
        help="أمر مخصص لمرحلة ما بعد الاستغلال.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        help="الحد الأقصى بالثواني لتنفيذ كل مرحلة مدعومة.",
    )
    parser.add_argument(
        "--output",
        help="ملف لحفظ النتائج بصيغة JSON.",
    )
    parser.add_argument(
        "--kali-defaults",
        action="store_true",
        help="تفعيل أوامر افتراضية متوافقة مع توزيعة Kali للمراحل الاختيارية.",
    )
    parser.add_argument(
        "-recon",
        "--recon",
        action="store_true",
        help="تشغيل مرحلة الاستطلاع (جمع معلومات)",
    )
    parser.add_argument(
        "-scan",
        "--scan",
        action="store_true",
        help="تشغيل الفحص باستخدام Nmap",
    )
    parser.add_argument(
        "--exploit",
        action="store_true",
        help="تشغيل مرحلة الاستغلال (اختبارات)",
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="مرحلة ما بعد الاستغلال",
    )
    parser.add_argument(
        "--mobile-scan",
        action="store_true",
        help="فحص خاص بالأجهزة المحمولة (MAC/OS/port scan)",
    )
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="تشغيل جميع المراحل كاملة",
    )

    args = parser.parse_args(argv)
    nmap_args = shlex.split(args.nmap_args) if args.nmap_args else []
    args.nmap_args_list = nmap_args
    return args


def ensure_stage_selection(args: argparse.Namespace) -> None:
    if any(
        [
            args.recon,
            args.scan,
            args.exploit,
            args.post,
            args.mobile_scan,
            args.full_scan,
        ]
    ):
        return
    raise StageExecutionError(
        "لم يتم اختيار أي مرحلة. استخدم --full-scan أو أحد الخيارات الفردية (مثل -recon أو --scan)."
    )


def execute_pipeline(args: argparse.Namespace) -> List[StageResult]:
    ensure_stage_selection(args)

    pipeline = SecurityPipeline(
        target=args.target,
        ports=args.ports,
        nmap_binary=args.nmap_binary,
        nmap_args=args.nmap_args_list,
        exploit_command=args.exploit_command,
        post_command=args.post_command,
        timeout=args.timeout,
    )

    stages_to_run: List[str] = []
    if args.full_scan:
        stages_to_run = ["recon", "scan", "exploit", "post", "mobile-scan"]
    else:
        if args.recon:
            stages_to_run.append("recon")
        if args.scan:
            stages_to_run.append("scan")
        if args.exploit:
            stages_to_run.append("exploit")
        if args.post:
            stages_to_run.append("post")
        if args.mobile_scan:
            stages_to_run.append("mobile-scan")

    results: List[StageResult] = []
    for stage in stages_to_run:
        try:
            if stage == "recon":
                results.append(pipeline.run_recon())
            elif stage == "scan":
                results.append(pipeline.run_standard_scan())
            elif stage == "exploit":
                results.append(pipeline.run_exploit())
            elif stage == "post":
                results.append(pipeline.run_post_exploitation())
            elif stage == "mobile-scan":
                results.append(pipeline.run_mobile_scan())
        except StageExecutionError as exc:
            results.append(
                StageResult(
                    name=stage,
                    success=False,
                    message=str(exc),
                    started_at=datetime.utcnow(),
                    finished_at=datetime.utcnow(),
                )
            )

    return results


def display_results(results: Sequence[StageResult]) -> None:
    for result in results:
        status = "✅" if result.success else "❌"
        print(f"[{status}] {result.name}: {result.message}")
        if result.command:
            print(f"    Command: {' '.join(result.command)}")
        if result.output:
            preview = result.output if len(result.output) <= 400 else result.output[:400] + "..."
            print("    Output:")
            print("        " + "\n        ".join(preview.splitlines()))
        if result.error:
            preview = result.error if len(result.error) <= 400 else result.error[:400] + "..."
            print("    Error:")
            print("        " + "\n        ".join(preview.splitlines()))
        print()


def write_results(results: Sequence[StageResult], path: Path) -> None:
    data = [result.to_dict() for result in results]
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = parse_args(argv)
        apply_kali_defaults(args)
        results = execute_pipeline(args)
        display_results(results)
        if args.output:
            write_results(results, Path(args.output))
        return 0 if all(result.success for result in results) else 1
    except StageExecutionError as exc:
        print(f"خطأ: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("تم إلغاء التنفيذ من قبل المستخدم.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
