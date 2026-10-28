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
        device = json.loads((examples / "device.json").read_text(encoding="utf-8"))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        device["agent"].update(host="127.0.0.1", port=port)
        device_path = Path(temporary) / "device.json"
        device_path.write_text(json.dumps(device), encoding="utf-8")
        target = f"127.0.0.1:{port}"

        def query(command: str, oid: str) -> str:
            return subprocess.run(
                [command, "-v2c", "-c", "public", "-t", "1", "-r", "0", "-On", target, oid],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout

        for scenario, oid, final_value, delay in (
            ("stable", CPU, 15, 0.1),
            ("temperature-jump", TEMPERATURE, 80, 5.2),
            ("cpu-step", CPU, 90, 8.2),
        ):
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
                        query("snmpget", CPU)
                        break
                    except subprocess.CalledProcessError:
                        if time.monotonic() >= deadline:
                            raise
                time.sleep(delay)
                result = query("snmpget", oid)
                if f"INTEGER: {final_value}" not in result:
                    raise RuntimeError(f"unexpected {scenario} value: {result}")
                walked = query("snmpwalk", "1.3.6.1.4.1.55555")
                if len(walked.strip().splitlines()) != 5:
                    raise RuntimeError(f"unexpected walk: {walked}")
                missing = query("snmpget", "1.3.6.1.4.1.55555.99.0")
                if "No Such Object" not in missing:
                    raise RuntimeError(f"unexpected unknown OID response: {missing}")
                print(f"Net-SNMP {scenario}: GET, WALK and unknown OID passed", flush=True)
            finally:
                process.terminate()
                process.communicate(timeout=5)


if __name__ == "__main__":
    main()
