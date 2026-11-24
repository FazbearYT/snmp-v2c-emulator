from __future__ import annotations

import json
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path

CPU = "1.3.6.1.4.1.55555.1.1.0"
TEMPERATURE = "1.3.6.1.4.1.55555.1.2.0"
DEMO_ROOT = "1.3.6.1.4.1.55555.4"
DEMO_CPU = f"{DEMO_ROOT}.1.0"


def main() -> None:
    examples = Path(sysconfig.get_path("data")) / "share/snmp-v2c-emulator/examples"
    with tempfile.TemporaryDirectory() as temporary:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        device_path = Path(temporary) / "device.json"
        target = f"127.0.0.1:{port}"

        def query(command: str, *oids: str, check: bool = True) -> subprocess.CompletedProcess:
            return subprocess.run(
                [command, "-v2c", "-c", "public", "-t", "0.3", "-r", "0", "-On", target, *oids],
                capture_output=True,
                text=True,
                check=check,
                timeout=5,
            )

        for device_name, scenario, oid, final_value, delay in (
            ("device.json", "stable", CPU, 15, 0.1),
            ("device.json", "temperature-jump", TEMPERATURE, 80, 5.2),
            ("device.json", "cpu-step", CPU, 90, 8.2),
            ("device-normal.json", "demo-normal", DEMO_CPU, 15, 0.1),
            ("device-normal.json", "demo-degradation", DEMO_CPU, 95, 12.2),
            ("device-emergency.json", "recovery", DEMO_CPU, 15, 12.2),
            ("device-normal.json", "device-unavailability", DEMO_CPU, 15, 2.1),
            ("device-normal.json", "partial-oid-unavailability", DEMO_CPU, 15, 0),
        ):
            device = json.loads((examples / device_name).read_text(encoding="utf-8"))
            device["agent"].update(host="127.0.0.1", port=port)
            device_path.write_text(json.dumps(device), encoding="utf-8")
            report_path = Path(temporary) / f"{scenario}-report.json"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "snmp_emulator",
                    "--device-config",
                    str(device_path),
                    "--scenario",
                    str(examples / f"{scenario}.json"),
                    "--report-file",
                    str(report_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=temporary,
                text=True,
            )
            try:
                deadline = time.monotonic() + 10
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"emulator exited: {process.stderr.read()}")
                    try:
                        initial = query("snmpget", oid).stdout
                        break
                    except subprocess.CalledProcessError:
                        if time.monotonic() >= deadline:
                            raise
                if scenario == "recovery" and "INTEGER: 95" not in initial:
                    raise RuntimeError(f"recovery did not start with emergency CPU: {initial}")
                if (
                    scenario in {"demo-degradation", "device-unavailability"}
                    and "INTEGER: 15" not in initial
                ):
                    raise RuntimeError(f"unexpected initial CPU for {scenario}: {initial}")
                time.sleep(delay)
                if scenario == "device-unavailability":
                    suppressed = query("snmpget", oid, check=False)
                    if suppressed.returncode == 0 or "timeout" not in suppressed.stderr.lower():
                        raise RuntimeError(f"expected Net-SNMP timeout: {suppressed}")
                    time.sleep(2)
                result = query("snmpget", oid).stdout
                if f"INTEGER: {final_value}" not in result:
                    raise RuntimeError(f"unexpected {scenario} value: {result}")
                if scenario == "partial-oid-unavailability":
                    temperature_oid = f"{DEMO_ROOT}.2.0"
                    description_oid = f"{DEMO_ROOT}.3.0"
                    status_oid = f"{DEMO_ROOT}.5.0"
                    missing = query("snmpget", DEMO_CPU, temperature_oid, description_oid).stdout
                    for expected in ("INTEGER: 15", "No Such Object", "No Such Instance"):
                        if expected not in missing:
                            raise RuntimeError(f"unexpected mixed GET response: {missing}")
                    failure = query("snmpget", DEMO_CPU, status_oid, check=False)
                    if (
                        failure.returncode == 0
                        or "generror" not in failure.stderr.lower()
                        or status_oid not in failure.stderr
                    ):
                        raise RuntimeError(f"unexpected genErr response: {failure}")
                    print(f"Net-SNMP {scenario}: GET exceptions and genErr passed", flush=True)
                else:
                    walked = query("snmpwalk", "1.3.6.1.4.1.55555").stdout
                    walked_oids = [
                        line.split(" = ", 1)[0]
                        for line in walked.splitlines()
                        if " = INTEGER: " in line or " = STRING: " in line
                    ]
                    expected_oids = [f".{metric['oid']}" for metric in device["metrics"]]
                    if walked_oids != expected_oids:
                        raise RuntimeError(f"unexpected walk: {walked}")
                    if scenario == "recovery":
                        for suffix, expected in (("2", "35"), ("5", "up"), ("6", "120")):
                            line = next(
                                line
                                for line in walked.splitlines()
                                if line.startswith(f".1.3.6.1.4.1.55555.4.{suffix}.0 = ")
                            )
                            if line.split(": ", 1)[1].strip('"') != expected:
                                raise RuntimeError(f"unexpected recovery metric: {line}")
                    missing = query("snmpget", "1.3.6.1.4.1.55555.99.0").stdout
                    if "No Such Object" not in missing:
                        raise RuntimeError(f"unexpected unknown OID response: {missing}")
                    print(f"Net-SNMP {scenario}: GET, WALK and unknown OID passed", flush=True)
            finally:
                process.terminate()
                _, errors = process.communicate(timeout=5)
            if process.returncode != 0:
                raise RuntimeError(f"emulator failed to stop after {scenario}: {errors}")
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report["scenario_id"] != scenario or report["outcome"] == "failed":
                raise RuntimeError(f"unexpected report for {scenario}: {report['outcome']}")
            if report["requests"]["sent"] == 0:
                raise RuntimeError(f"report for {scenario} has no SNMP responses")
            if (
                scenario == "device-unavailability"
                and not report["requests"]["intentionally_suppressed"]
            ):
                raise RuntimeError("outage report did not record a suppressed request")
            if scenario == "partial-oid-unavailability":
                for counter in ("no_such_object", "no_such_instance", "simulated_errors"):
                    if report["requests"][counter] == 0:
                        raise RuntimeError(f"partial outage report did not record {counter}")


if __name__ == "__main__":
    main()
