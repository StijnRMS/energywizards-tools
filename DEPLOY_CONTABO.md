# ⚡ EnergyWizards — Deploy on Contabo VPS
**File:** `reconcile_dashboard.py`  
**Target:** Contabo VPS (Ubuntu 22.04 / 24.04)  
**Result:** Dashboard always running at `http://YOUR-IP:5757` or on a domain with HTTPS

---

## 1. Buy & Access Your Contabo VPS

1. Go to **contabo.com** → choose any VPS (Cloud VPS S or M is enough)
2. Select **Ubuntu 22.04** as OS
3. After purchase you get an email with:
   - IP address (e.g. `85.215.xxx.xxx`)
   - Root password
4. Connect via SSH:
```bash
ssh root@85.215.xxx.xxx
```
Enter the password from the email.

---

## 2. First-time Server Setup

```bash
# Update system
apt update && apt upgrade -y

# Install Python (already on Ubuntu, but make sure)
apt install -y python3 python3-pip git screen ufw

# Create a dedicated user (safer than running as root)
adduser energywizards
# Follow prompts, set a password
usermod -aG sudo energywizards

# Switch to the new user
su - energywizards
```

---

## 3. Clone Your GitHub Repo

```bash
cd ~
git clone https://github.com/StijnRMS/energywizards-tools.git
cd energywizards-tools
ls
# You should see: reconcile_dashboard.py
```

---

## 4. Test Run

```bash
python3 reconcile_dashboard.py
```

You should see:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 ⚡ EnergyWizards — Bank Reconciliation Dashboard
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Server running at http://localhost:5757
```

Press `Ctrl+C` to stop — we'll run it properly in the next step.

---

## 5. Open Firewall Port

```bash
# Allow port 5757 through UFW firewall
sudo ufw allow 5757
sudo ufw allow 22      # keep SSH open!
sudo ufw enable
sudo ufw status
```

Also open port **5757** in the **Contabo control panel**:
1. Log into contabo.com → **Your Services**
2. Click your VPS → **Firewall**
3. Add inbound rule: **TCP port 5757**, source **0.0.0.0/0**

---

## 6. Make It Listen on All Interfaces

By default the dashboard only listens on `127.0.0.1` (localhost).  
On a server, change it to `0.0.0.0` so it's reachable from outside.

Edit line in `reconcile_dashboard.py`:
```bash
nano reconcile_dashboard.py
```
Find this line (near the bottom, in the `main()` function):
```python
server = HTTPServer(("127.0.0.1", PORT), Handler)
```
Change to:
```python
server = HTTPServer(("0.0.0.0", PORT), Handler)
```
Save: `Ctrl+O` → Enter → `Ctrl+X`

---

## 7. Run Permanently with Screen

`screen` keeps the process running after you close SSH.

```bash
# Start a named screen session
screen -S reconcile

# Run the dashboard
python3 reconcile_dashboard.py

# Detach from screen (process keeps running)
# Press: Ctrl+A  then  D
```

To come back to it later:
```bash
screen -r reconcile
```

To see all running screens:
```bash
screen -ls
```

---

## 8. Access the Dashboard

Open your browser and go to:
```
http://85.215.xxx.xxx:5757
```
Replace `85.215.xxx.xxx` with your actual Contabo IP.

---

## 9. Auto-Start on Reboot (systemd service)

So it restarts automatically if the server reboots:

```bash
sudo nano /etc/systemd/system/reconcile.service
```

Paste this (replace `energywizards` with your username if different):
```ini
[Unit]
Description=EnergyWizards Bank Reconciliation Dashboard
After=network.target

[Service]
Type=simple
User=energywizards
WorkingDirectory=/home/energywizards/energywizards-tools
ExecStart=/usr/bin/python3 reconcile_dashboard.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Save: `Ctrl+O` → Enter → `Ctrl+X`

```bash
# Enable and start the service
sudo systemctl daemon-reload
sudo systemctl enable reconcile
sudo systemctl start reconcile

# Check it's running
sudo systemctl status reconcile
```

---

## 10. (Optional) Add a Domain + HTTPS with Nginx

If you have a domain (e.g. `tools.energywizards.be`):

```bash
# Install Nginx and Certbot
sudo apt install -y nginx certbot python3-certbot-nginx

# Point your domain's DNS A record to your Contabo IP first, then:
sudo certbot --nginx -d tools.energywizards.be
```

Create Nginx config:
```bash
sudo nano /etc/nginx/sites-available/reconcile
```

Paste:
```nginx
server {
    listen 80;
    server_name tools.energywizards.be;

    location / {
        proxy_pass http://127.0.0.1:5757;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/reconcile /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

Now access via: `https://tools.energywizards.be`  
Certbot auto-renews the SSL certificate every 90 days.

---

## 11. Update the App (pull latest from GitHub)

Whenever a new version is pushed from Claude:

```bash
cd ~/energywizards-tools
git pull origin main
sudo systemctl restart reconcile
```

That's it — one command to update.

---

## Quick Reference

| Task | Command |
|------|---------|
| SSH into server | `ssh energywizards@85.215.xxx.xxx` |
| Start dashboard | `sudo systemctl start reconcile` |
| Stop dashboard | `sudo systemctl stop reconcile` |
| Restart dashboard | `sudo systemctl restart reconcile` |
| Check status | `sudo systemctl status reconcile` |
| View live logs | `sudo journalctl -fu reconcile` |
| Update from GitHub | `cd ~/energywizards-tools && git pull && sudo systemctl restart reconcile` |
| Open firewall port | `sudo ufw allow 5757` |

---

## Minimum Contabo VPS Specs Needed

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 1 vCore | 2 vCores |
| RAM | 2 GB | 4 GB |
| Storage | 20 GB | 50 GB |
| OS | Ubuntu 22.04 | Ubuntu 22.04 LTS |
| Monthly cost | ~€4–6 | ~€8–12 |

The dashboard uses minimal resources — even the cheapest Contabo VPS is more than enough.

---

*Generated by Claude — EnergyWizards Agent Boekhouding*  
*Last updated: April 2026*
