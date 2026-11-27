# Deploy HVAC MAS on an Oracle Cloud VM

This deploys the current backend as two persistent `systemd` services on one Ubuntu VM: FastAPI and the SPADE runtime. MongoDB Atlas remains the database, and Caddy terminates HTTPS for the API. No Docker is required.

## Before creating the VM

- Create or push the backend repository to GitHub so the VM can download its source.
- Have the Vercel production hostname and a DNS name for the API, such as `api.example.com`. The DNS name must point to the VM before Caddy can obtain an HTTPS certificate.
- Have the MongoDB Atlas URI available. Keep it private and do not commit it.

## 1. Create the VM in OCI

In the OCI Console, use **Compute → Instances → Create instance**:

1. Choose an Ubuntu 24.04 image for the Arm-based `VM.Standard.A1.Flex` shape. Use only Always Free eligible CPU, memory, and boot volume amounts shown in your account. Oracle currently lists an Always Free allowance equivalent to 2 OCPUs and 12 GB of memory, but A1 capacity may be unavailable in some home regions.
2. Create or select a VCN with a public subnet and Internet Gateway, assign a public IP, and add your SSH public key.
3. In the subnet security list or the instance NSG, allow inbound TCP **22** only from your current public IP; allow TCP **80** and **443** from the internet for HTTP-to-HTTPS and HTTPS traffic. Do not expose ports **8000** or **5222**.
4. Before creating resources, review OCI's cost estimate and confirm each selected resource is marked Always Free. Oracle may reclaim Always Free instances that remain below its CPU, network, and (for A1) memory thresholds for seven days; this is suitable for a demo, not an uptime guarantee.

Oracle's current Always Free allowance and idle reclamation rules are documented [here](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm). The instance also needs a public IP, an internet route, and permitted ingress rules to receive internet traffic; see [Oracle's instance networking requirements](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/launchinginstance.htm).

## 2. Point the API hostname at the VM

At your DNS provider, create an `A` record for the API hostname (for example `api.example.com`) pointing to the VM's public IPv4 address. If the address changes, update DNS. Wait until the hostname resolves before installing Caddy in step 6.

## 3. Connect and install the backend

From your Mac, connect with the private key you selected when creating the VM:

```sh
ssh -i ~/.ssh/oci_hvac_mas.key ubuntu@YOUR_VM_PUBLIC_IP
```

On the VM, install prerequisites and create the service account:

```sh
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip python3-dev build-essential
sudo useradd --system --create-home --home-dir /opt/hvac-mas --shell /usr/sbin/nologin hvacmas
```

If you enable Ubuntu's UFW firewall, allow SSH before enabling it, then allow web traffic:

```sh
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

Clone the backend repository into the service directory, then install it in a virtual environment. Replace the URL with your backend repository URL:

```sh
sudo git clone https://github.com/YOUR_ACCOUNT/YOUR_BACKEND_REPO.git /opt/hvac-mas
sudo chown -R hvacmas:hvacmas /opt/hvac-mas
sudo -u hvacmas python3 -m venv /opt/hvac-mas/.venv
sudo -u hvacmas /opt/hvac-mas/.venv/bin/pip install --upgrade pip
sudo -u hvacmas /opt/hvac-mas/.venv/bin/pip install /opt/hvac-mas
```

The repository's root contains `pyproject.toml` and `config/`, which the installation and services expect. Set the working directory to `/opt/hvac-mas` as shown; the application loads its YAML configuration relative to that directory.

## 4. Add private backend settings

Create a root-owned environment file outside the repository:

```sh
sudo install -d -m 700 /etc/hvac-mas
sudo nano /etc/hvac-mas/hvac-mas.env
```

Copy the variable names from `deploy/oracle/hvac-mas.env.example` and replace the placeholders. Set `MONGODB_URI` to your Atlas connection string, `CORS_ORIGINS` to the exact Vercel origin, and set `DEPLOYMENT_MODE=hosted` with distinct random values for `RUNTIME_TOKEN` and `EVALUATOR_TOKEN`. Use the same `RUNTIME_TOKEN` for both services; `API_URL` should remain `http://127.0.0.1:8000` because the worker and API share this VM.

If your MongoDB password contains reserved URI characters, use the URL-encoded password from Atlas or percent-encode those characters before setting `MONGODB_URI`.

Generate each token independently with `openssl rand -hex 32`, paste them into the VM environment file, then secure that file:

```sh
sudo chown root:root /etc/hvac-mas/hvac-mas.env
sudo chmod 600 /etc/hvac-mas/hvac-mas.env
```

In MongoDB Atlas Network Access, allow the VM's outbound IP address to connect to the cluster. Do not use `0.0.0.0/0` unless you knowingly accept allowing connections from every IPv4 address.

## 5. Install and start the persistent API and SPADE services

From the backend repository on the VM, install the included systemd unit files and start both services:

```sh
sudo install -m 644 /opt/hvac-mas/deploy/oracle/hvac-mas-api.service /etc/systemd/system/
sudo install -m 644 /opt/hvac-mas/deploy/oracle/hvac-mas-spade.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hvac-mas-api.service hvac-mas-spade.service
```

Check service status and logs:

```sh
sudo systemctl status hvac-mas-api.service hvac-mas-spade.service
sudo journalctl -u hvac-mas-api.service -u hvac-mas-spade.service -n 100 --no-pager
```

The API listens only on `127.0.0.1:8000`; Caddy will be the public HTTPS entry point. The SPADE service uses the embedded XMPP server and communicates with the API on the same VM.

## 6. Enable HTTPS with Caddy

Install Caddy using its official Ubuntu package instructions: [Caddy installation](https://caddyserver.com/docs/install). Then copy the example file, replace the hostname with the DNS name pointing to your VM, validate it, and reload Caddy:

```sh
sudo cp /opt/hvac-mas/deploy/oracle/Caddyfile.example /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy obtains and renews the TLS certificate when DNS and ingress ports 80/443 are correct. Its `reverse_proxy` directive forwards the public HTTPS requests to the local API on port 8000; see [Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).

## 7. Connect Vercel and verify

In Vercel → Project Settings → Environment Variables, set `VITE_API_URL` to `https://api.example.com` (your actual API hostname) for Production, save, then redeploy. Environment variable changes apply to new deployments, so redeployment is required. The backend's `CORS_ORIGINS` must contain the exact frontend origin, such as `https://your-project.vercel.app`.

Check the endpoint:

```sh
curl https://api.example.com/health
```

Wait for `agent_ready` to report `true`, then load the Vercel site, enter the evaluator access key, and confirm the dashboard says **Agents connected**. Run only one API service and one SPADE worker; the current system uses in-process coordination and has one shared simulation.

## Evaluator access

Arrange one authorized demonstration at a time. The evaluator key grants control of the shared run. Give the evaluator only `EVALUATOR_TOKEN` through a private channel, and rotate it after the demonstration by updating the server environment file and restarting both services. The worker endpoints are available to the local worker and blocked at Caddy.
