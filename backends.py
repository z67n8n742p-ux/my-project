"""Terminal backends (Hermes pattern, laptop-first).

Interface: run(cmd, workdir, timeout) -> (stdout, stderr, returncode).

Backends:
  local – subprocess on this machine (default, zero deps).
  docker – run inside a container via `docker exec` (needs FORMY_DOCKER_CONTAINER).
  ssh    – run on a remote box via `ssh` (needs FORMY_SSH_TARGET, e.g. user@host).

Select with FORMY_BACKEND=local|docker|ssh (default local).
Unknown value falls back to local – never breaks the agent.
"""
import os
import shutil
import subprocess

BACKEND = os.getenv("FORMY_BACKEND", "local").strip().lower() or "local"


def available_backend(name: str) -> bool:
    name = (name or "").lower()
    if name == "local":
        return True
    if name == "docker":
        if not shutil.which("docker"):
            return False
        if not os.getenv("FORMY_DOCKER_CONTAINER", "").strip():
            return False
        return True
    if name == "ssh":
        if not shutil.which("ssh"):
            return False
        if not os.getenv("FORMY_SSH_TARGET", "").strip():
            return False
        return True
    return False


def _run_local(command: str, workdir: str, timeout: int):
    p = subprocess.run(
        command, shell=True, cwd=workdir or ".",
        capture_output=True, text=True, timeout=timeout,
    )
    return p.stdout or "", p.stderr or "", p.returncode


def _run_docker(command: str, workdir: str, timeout: int):
    container = os.getenv("FORMY_DOCKER_CONTAINER", "").strip()
    # sh -c passthrough; workdir honored inside container when possible
    inner = command.replace('"', '\\"')
    docker_cmd = f'docker exec -i {container} sh -c "{inner}"'
    p = subprocess.run(
        docker_cmd, shell=True, cwd=workdir or ".",
        capture_output=True, text=True, timeout=timeout,
    )
    return p.stdout or "", p.stderr or "", p.returncode


def _run_ssh(command: str, workdir: str, timeout: int):
    target = os.getenv("FORMY_SSH_TARGET", "").strip()
    inner = command.replace("'", "'\\''")
    ssh_cmd = f"ssh -o BatchMode=yes -o ConnectTimeout=10 {target} '{inner}'"
    p = subprocess.run(
        ssh_cmd, shell=True, cwd=workdir or ".",
        capture_output=True, text=True, timeout=timeout,
    )
    return p.stdout or "", p.stderr or "", p.returncode


def run(command: str, workdir: str = ".", timeout: int = 120):
    """Run a shell command on the selected backend. Always returns 3-tuple."""
    backend = BACKEND
    if backend == "docker" and available_backend("docker"):
        return _run_docker(command, workdir, timeout)
    if backend == "ssh" and available_backend("ssh"):
        return _run_ssh(command, workdir, timeout)
    return _run_local(command, workdir, timeout)


def describe() -> str:
    return (
        f"backend={BACKEND} "
        f"(local=yes docker={'yes' if available_backend('docker') else 'no'} "
        f"ssh={'yes' if available_backend('ssh') else 'no'})"
    )
