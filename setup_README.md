# Flower Prototype Setup on Windows

Runs one coordinator (SuperLink) and three clients (SuperNodes) with dummy model updates. Cowrie and real intrusion-detection training are not integrated yet.

## Prerequisites

- Python 3.12
- Docker Desktop running with Linux containers (WSL 2 backend)
- Extract the project ZIP and open the project root in VS Code.

Use PowerShell for the commands below.

## 1. Check Docker

```powershell
docker version
docker compose version
```

`docker version` must show both Client and Server. If the engine is unavailable, open Docker Desktop and wait for it to start.

## 2. Install Python dependencies

From the project root:

```powershell
cd .\prototypes\andrew-flower\quickstart-numpy
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e . "flwr==1.40.0"
.\.venv\Scripts\flwr.exe --version
```

Keep Flower 1.40.0 to match the Docker images. Do not install the root `requirements.txt` for this prototype.

## 3. Configure the connection

```powershell
.\.venv\Scripts\flwr.exe config list
```

Open the `config.toml` path printed by Flower. Add this section once and save:

```toml
[superlink.local-deployment]
address = "127.0.0.1:8000"
insecure = true
```

This connection is for local testing only.

## 4. Start the containers

```powershell
cd ..\flower-docker
docker compose up --build -d
docker compose ps
```

Check that `superlink`, `supernode-a`, `supernode-b` and `supernode-c` are running.

## 5. Run the application

```powershell
cd ..\quickstart-numpy
.\.venv\Scripts\flwr.exe run . local-deployment --stream
```

The prototype runs three rounds and requires at least two successful updates per round. Simulated failures and retries are expected. A successful run ends with `Training completed in ... seconds.`

## Logs and shutdown

From `flower-docker`:

```powershell
docker compose logs --tail=100
docker compose down
```

To rerun while the containers are running, repeat the application command in Step 5.
