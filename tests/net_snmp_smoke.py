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


def main() -> None:
    examples = Path(sysconfig.get_path("data")) / "share/snmp-v2c-emulator/examples"
    with tempfile.TemporaryDirectory() as temporary:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        device_path = Path(temporary) / "device.json"
        target = f"127.0.0.1:{port}"

        def query(command: str, oid: str) -> str:
            return subprocess.run(
                [command, "-v2c", "-c", "public", "-t", "1", "-r", "0", "-On", target, oid],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout

        for device_name, scenario, oid, final_value, delay in (
            ("device.json", "stable", CPU, 15, 0.1),
            ("device.json", "temperature-jump", TEMPERATURE, 80, 5.2),
            ("device.json", "cpu-step", CPU, 90, 8.2),
            ("device-emergency.json", "recovery", "1.3.6.1.4.1.55555.4.1.0", 15, 12.2),
        ):
            device = json.loads((examples / device_name).read_text(encoding="utf-8"))
            device["agent"].update(host="127.0.0.1", port=port)
            device_path.write_text(json.dumps(device), encoding="utf-8")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "snmp_emulator",
                    "--device-config",
                    str(device_path),
                    "--scenario",
                    str(examples / f"{scenario}.json"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 10
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"emulator exited: {process.stderr.read()}")
                    try:
                        initial = query("snmpget", oid)
                        break
                    except subprocess.CalledProcessError:
                        if time.monotonic() >= deadline:
                            raise
                if scenario == "recovery" and "INTEGER: 95" not in initial:
                    raise RuntimeError(f"recovery did not start with emergency CPU: {initial}")
                time.sleep(delay)
                result = query("snmpget", oid)
                if f"INTEGER: {final_value}" not in result:
                    raise RuntimeError(f"unexpected {scenario} value: {result}")
                walked = query("snmpwalk", "1.3.6.1.4.1.55555")
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
                missing = query("snmpget", "1.3.6.1.4.1.55555.99.0")
                if "No Such Object" not in missing:
                    raise RuntimeError(f"unexpected unknown OID response: {missing}")
                print(f"Net-SNMP {scenario}: GET, WALK and unknown OID passed", flush=True)
            finally:
                process.terminate()
                process.communicate(timeout=5)


if __name__ == "__main__":
    main()
