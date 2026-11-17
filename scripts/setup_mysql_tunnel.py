"""Install a restricted reverse-tunnel key and a Docker-private TCP relay."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime
import hashlib
import ipaddress
import json
from pathlib import Path
import subprocess
import time

MARKER = "datapilot-mac-mysql-tunnel"
CONTAINER = "da-mysql-tunnel-proxy"

# A per-key permitopen/permitlisten does not restrict Unix-socket forwarding.
# Scope those restrictions to the existing deployment user; deployment uses
# normal SSH sessions/SFTP, which these two forwarding settings do not change.
SSHD_RESTRICTIONS = r'''
import os, pathlib, signal, subprocess, time
path = pathlib.Path('/etc/ssh/sshd_config')
original = path.read_text()
marker = '# DataPilot local MySQL forwarding restrictions'
old_block = chr(10).join(['', marker, 'Match User github-deploy',
                         '    AllowTcpForwarding remote', '    AllowStreamLocalForwarding no', ''])
block = chr(10).join(['', marker, 'Match User github-deploy',
                     '    AllowTcpForwarding remote', '    AllowStreamLocalForwarding no',
                     '    ClientAliveInterval 30', '    ClientAliveCountMax 3', ''])
if marker in original and old_block not in original and block not in original:
    raise ValueError('Existing managed sshd block has unexpected contents')
updated = original if block in original else original.replace(old_block, '', 1) + block
changed = updated != original
reloaded = False
if changed:
    backup = pathlib.Path('/etc/ssh/sshd_config.before-datapilot-' + str(time.time_ns()))
    backup.write_text(original)
    backup.chmod(0o600)
    path.write_text(updated)
try:
    subprocess.run(['/usr/sbin/sshd', '-t'], check=True)
    effective = subprocess.check_output([
        '/usr/sbin/sshd', '-T', '-C', 'user=github-deploy,host=localhost,addr=127.0.0.1'
    ], text=True)
    settings = dict(line.split(' ', 1) for line in effective.splitlines())
    assert settings['allowtcpforwarding'] == 'remote', 'TCP forwarding restriction was not applied'
    assert settings['allowstreamlocalforwarding'] == 'no', 'Unix forwarding must be disabled'
    assert settings['clientaliveinterval'] == '30' and settings['clientalivecountmax'] == '3'
    if changed:
        pid = int(pathlib.Path(settings['pidfile']).read_text().strip())
        if pid <= 1:
            raise ValueError('Invalid sshd master PID')
        os.kill(pid, signal.SIGHUP)
        reloaded = True
        time.sleep(0.5)
        os.kill(pid, 0)
except BaseException:
    if changed:
        path.write_text(original)
        subprocess.run(['/usr/sbin/sshd', '-t'], check=True)
        if reloaded:
            try:
                os.kill(pid, signal.SIGHUP)
            except ProcessLookupError:
                subprocess.run(['systemctl', 'restart', 'ssh.service'], check=True)
    raise
print('sshd validated: deployment user permits remote TCP forwarding only; Unix forwarding disabled')
'''


def authorized_key(key_base64: str) -> str:
    blob = base64.b64decode(key_base64, validate=True)
    if len(blob) != 51 or blob[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
        raise ValueError("Expected an Ed25519 public key")
    # Permit only the intended reverse listener. Any attempted session exits;
    # local forwarding is restricted to an unused loopback destination.
    return ("restrict,port-forwarding,permitlisten=\"127.0.0.1:13306\","
            "permitopen=\"127.0.0.1:1\",command=\"/bin/false\" "
            f"ssh-ed25519 {key_base64} {MARKER}")


def install_key(key_base64: str, ssh_dir: Path) -> None:
    entry = authorized_key(key_base64)
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = ssh_dir / "authorized_keys"
    old = path.read_text() if path.exists() else ""
    lines = [line for line in old.splitlines() if not line.endswith(" " + MARKER)]
    updated = "\n".join([*lines, entry]) + "\n"
    if old == updated:
        return
    if path.exists():
        backup = ssh_dir / ("authorized_keys.before-mysql-tunnel-" + datetime.now().strftime("%Y%m%d%H%M%S%f"))
        backup.write_text(old)
        backup.chmod(0o600)
    temporary = ssh_dir / ".authorized_keys.mysql-tunnel.tmp"
    temporary.touch(mode=0o600, exist_ok=True)
    temporary.chmod(0o600)
    temporary.write_text(updated)
    temporary.replace(path)


def docker_json(*args):
    return json.loads(subprocess.check_output(["docker", *args], text=True))


def restrict_forwarding() -> None:
    image = docker_json("inspect", "da-backend")[0]["Image"]
    # The deployment account already administers Docker. Run this short-lived
    # helper as root solely to update and validate sshd configuration, retaining
    # a backup. No deployment private key is copied to the Mac.
    bootstrap = ("import os,sys; os.chroot('/host'); os.chdir('/'); "
                 "os.execv('/usr/bin/python3',['python3','-c',sys.argv[1]])")
    subprocess.run([
        "docker", "run", "--rm", "--user", "0:0", "--pid", "host", "--network", "none",
        "--cap-drop", "ALL", "--cap-add", "SYS_CHROOT", "--cap-add", "KILL",
        "--security-opt", "apparmor=unconfined",
        "--security-opt", "no-new-privileges=true",
        "--mount", "type=bind,src=/,dst=/host", "--entrypoint", "python", image,
        "-c", bootstrap, SSHD_RESTRICTIONS,
    ], check=True)


def install_proxy() -> dict:
    backend = docker_json("inspect", "da-backend")[0]
    networks = backend["NetworkSettings"]["Networks"]
    if len(networks) != 1:
        raise ValueError("Expected one backend network; inspect routing before installing")
    network_name = next(iter(networks))
    network = docker_json("network", "inspect", network_name)[0]
    ipv4 = next(c for c in network["IPAM"]["Config"] if ":" not in c["Subnet"])
    gateway = ipaddress.ip_address(ipv4["Gateway"])
    subnet = ipaddress.ip_network(ipv4["Subnet"])
    if not gateway.is_private or gateway.is_loopback or gateway not in subnet:
        raise ValueError("Refusing to expose the relay outside the Docker private network")
    source = Path(__file__).resolve().with_name("mysql_tunnel_proxy.py")
    source.chmod(0o644)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    labels = {"datapilot.role": "mysql-tunnel", "datapilot.source": digest,
              "datapilot.gateway": str(gateway)}
    existing = subprocess.run(["docker", "inspect", CONTAINER], capture_output=True, text=True)
    if existing.returncode == 0:
        current = json.loads(existing.stdout)[0]
        current_labels = current["Config"].get("Labels") or {}
        if current_labels.get("datapilot.role") != "mysql-tunnel":
            raise ValueError("Container name is already in use by another service")
        if all(current_labels.get(k) == v for k, v in labels.items()) and current["Image"] == backend["Image"]:
            subprocess.run(["docker", "start", CONTAINER], check=True, stdout=subprocess.DEVNULL)
        else:
            subprocess.run(["docker", "rm", "-f", CONTAINER], check=True, stdout=subprocess.DEVNULL)
            existing = None
    else:
        existing = None
    if existing is None:
        command = ["docker", "run", "-d", "--name", CONTAINER,
                   "--restart", "unless-stopped", "--network", "host", "--read-only",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
                   "--pids-limit", "128", "--memory", "64m", "--cpus", "0.25",
                   "--log-opt", "max-size=5m", "--log-opt", "max-file=2"]
        for name, value in labels.items():
            command.extend(["--label", f"{name}={value}"])
        command.extend(["--mount", f"type=bind,src={source},dst=/mysql_tunnel_proxy.py,readonly",
                        "--entrypoint", "python", backend["Image"], "-u", "/mysql_tunnel_proxy.py",
                        "--bind", str(gateway), "--port", "13307", "--allow-network", str(subnet)])
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
    time.sleep(1)
    status = docker_json("inspect", CONTAINER)[0]["State"]
    if not status["Running"] or status.get("Restarting"):
        raise RuntimeError("Tunnel relay did not start; inspect its container logs")
    return {"host": str(gateway), "port": 13307, "allowed_network": str(subnet),
            "reverse_listener": "127.0.0.1:13306", "container": CONTAINER}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-key-base64", required=True)
    args = parser.parse_args()
    authorized_key(args.public_key_base64)
    restrict_forwarding()
    install_key(args.public_key_base64, Path.home() / ".ssh")
    print(json.dumps(install_proxy()))


if __name__ == "__main__":
    main()
