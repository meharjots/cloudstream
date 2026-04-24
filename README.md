# CloudStream — CW2 Implementation Plan

**Module:** COM682 Cloud Native Development
**Student:** Meharjot Singh (B00963621)
**Project:** CloudStream — scalable cloud-native multimedia sharing platform
**Platform:** Microsoft Azure · **Region:** Birmingham

---

## 1. Architecture recap (from CW1)

| Layer | Azure service | Role |
|---|---|---|
| Frontend | Azure Static Web App | HTML/JS UI, hosted at the edge |
| API (CRUD) | Azure Functions (Python, Consumption) | REST endpoints for media metadata |
| Media storage | Azure Blob Storage | Binary files (images/videos) |
| Metadata store | Azure Cosmos DB (NoSQL, free tier) | Users + Media metadata |
| Workflow | Azure Logic App | Async pipeline on blob upload |
| Monitoring | Application Insights | Logs, metrics, alerts |
| Secrets | Key Vault | Connection strings / keys |
| CI/CD | GitHub Actions | Auto-deploy Functions + SWA |

---

## 2. Grade-maxing strategy (mapped to CW2 rubric)

| Rubric criterion | Weight | What earns full marks | Where in this plan |
|---|---|---|---|
| Implementation | 35% | All CRUD operations work end-to-end, frontend + backend integrated | Phases 2, 3, 7 |
| Use of Azure Resources | 35% | All services correctly deployed, integrated via managed identity, Logic Apps used for real workflow | Phases 1, 4 |
| Use of Advanced Features | 20% | Managed Identity, Key Vault, App Insights alerts, autoscale, Event Grid, Content Safety (pick 4+) | Phase 5 |
| Video Quality | 10% | Under 5 min, camera on, walks through Azure portal + app + CI/CD | Phase 8 |

**Jumping from 2.1 → 1st:** the single biggest differentiator is Phase 5 (advanced features). Treat it as mandatory, not optional.

**Critical fix from CW1:** your deck said Functions handle the API and Logic Apps do "workflow automation." The CW2 brief asks for Logic Apps in the REST path. We reconcile this by:
- Functions handle CRUD (low latency, correct fit) — you justify this on video
- Logic Apps handle a real async workflow triggered on blob upload (notification, thumbnail, or moderation)

Both services get a legitimate role. Say this out loud on the video.

---

## 3. Cost budget

| Service | Expected usage | Estimated cost |
|---|---|---|
| Cosmos DB (free tier) | 1000 RU/s, <1 GB | £0 |
| Functions (Consumption) | <1000 invocations for demo | £0 |
| Static Web Apps (Free) | 1 app, <100 GB bandwidth | £0 |
| Application Insights | <1 GB logs | £0 |
| Blob Storage | <1 GB hot LRS | ~£0.02 |
| Logic App (Consumption) | <100 runs | <£0.01 |
| Key Vault | <1000 ops | <£0.05 |
| **Total** | | **<£1 of your £100 credit** |

Always tear down the resource group after marking if you don't need it: `az group delete -n cloudstream-rg --yes`.

---

## 4. Prerequisites — install once

On your machine:
- **Python 3.11** (Functions v4 runtime supports 3.10 / 3.11)
- **Azure CLI** — https://learn.microsoft.com/cli/azure/install-azure-cli
- **Azure Functions Core Tools v4** — `npm i -g azure-functions-core-tools@4 --unsafe-perm true`
- **VS Code** + extensions: "Azure Functions", "Azure Static Web Apps", "Python"
- **Git** + a **GitHub account** (repo will drive CI/CD)
- **Node.js LTS** (needed by Functions Core Tools and SWA CLI)

Sign into Azure CLI once:
```bash
az login
az account show   # confirm you're on the Azure for Students subscription
```

---

## 5. Phase 1 — Resource provisioning (run today)

Create a file `provision.sh` in your repo root, paste this, then run section by section. Do **not** run it all at once the first time — watch each output for errors.

```bash
# --- variables (edit the suffix to something unique to you) ---
SUFFIX=ms963621          # keep short, lowercase, no symbols
RG=cloudstream-rg
LOC=uksouth

STORAGE=cloudstreamst$SUFFIX        # 3-24 chars, lowercase, globally unique
COSMOS=cloudstream-cosmos-$SUFFIX
FUNCAPP=cloudstream-api-$SUFFIX
AI=cloudstream-insights
KV=cloudstream-kv-$SUFFIX           # 3-24 chars, globally unique
LOGICAPP=cloudstream-workflow

# --- 1. Resource group ---
az group create -n $RG -l $LOC

# --- 2. Storage account (hosts blobs AND the Functions runtime files) ---
az storage account create \
  -n $STORAGE -g $RG -l $LOC \
  --sku Standard_LRS \
  --allow-blob-public-access true \
  --min-tls-version TLS1_2

# --- 3. Blob containers ---
# Get connection string to create containers
STORAGE_CONN=$(az storage account show-connection-string -n $STORAGE -g $RG --query connectionString -o tsv)

az storage container create --name media --connection-string "$STORAGE_CONN" --public-access blob
az storage container create --name thumbnails --connection-string "$STORAGE_CONN" --public-access blob

# Enable CORS so the frontend can upload directly if needed
az storage cors add --services b --methods GET POST PUT DELETE OPTIONS \
  --origins '*' --allowed-headers '*' --exposed-headers '*' --max-age 3600 \
  --connection-string "$STORAGE_CONN"

# --- 4. Cosmos DB with FREE TIER (one per subscription!) ---
az cosmosdb create \
  -n $COSMOS -g $RG \
  --kind GlobalDocumentDB \
  --enable-free-tier true \
  --default-consistency-level Session \
  --locations regionName=$LOC failoverPriority=0

az cosmosdb sql database create -a $COSMOS -g $RG -n CloudStreamDB \
  --throughput 1000   # shared among containers, stays in free 1000 RU/s

az cosmosdb sql container create -a $COSMOS -g $RG -d CloudStreamDB \
  -n users --partition-key-path "/userId"

az cosmosdb sql container create -a $COSMOS -g $RG -d CloudStreamDB \
  -n media --partition-key-path "/userId"

# --- 5. Application Insights (log-based alerts later) ---
az extension add --name application-insights --yes
az monitor app-insights component create --app $AI -g $RG -l $LOC --kind web

AI_KEY=$(az monitor app-insights component show --app $AI -g $RG \
  --query instrumentationKey -o tsv)

# --- 6. Function App (Python 3.11, Consumption, managed identity ON) ---
az functionapp create \
  -n $FUNCAPP -g $RG \
  --storage-account $STORAGE \
  --consumption-plan-location $LOC \
  --runtime python --runtime-version 3.11 \
  --functions-version 4 \
  --os-type Linux \
  --app-insights $AI \
  --assign-identity        # enables system-assigned managed identity

# --- 7. Key Vault (RBAC mode, not access policies) ---
az keyvault create -n $KV -g $RG -l $LOC \
  --enable-rbac-authorization true

# Grant YOU permission to manage secrets
ME=$(az ad signed-in-user show --query id -o tsv)
az role assignment create \
  --assignee $ME \
  --role "Key Vault Secrets Officer" \
  --scope $(az keyvault show -n $KV -g $RG --query id -o tsv)

# Grant the Function App's managed identity permission to READ secrets
FUNC_PRINCIPAL=$(az functionapp identity show -n $FUNCAPP -g $RG --query principalId -o tsv)
az role assignment create \
  --assignee $FUNC_PRINCIPAL \
  --role "Key Vault Secrets User" \
  --scope $(az keyvault show -n $KV -g $RG --query id -o tsv)

# --- 8. Grant Function App managed identity access to Cosmos & Blob ---
# Cosmos DB data plane (custom role — the simple built-in is "Cosmos DB Built-in Data Contributor")
COSMOS_ID=$(az cosmosdb show -n $COSMOS -g $RG --query id -o tsv)
az cosmosdb sql role assignment create \
  --account-name $COSMOS -g $RG \
  --scope $COSMOS_ID \
  --principal-id $FUNC_PRINCIPAL \
  --role-definition-id 00000000-0000-0000-0000-000000000002  # Built-in Data Contributor

# Blob data plane
STORAGE_ID=$(az storage account show -n $STORAGE -g $RG --query id -o tsv)
az role assignment create \
  --assignee $FUNC_PRINCIPAL \
  --role "Storage Blob Data Contributor" \
  --scope $STORAGE_ID

echo "Provisioning complete."
echo "Function app:  https://$FUNCAPP.azurewebsites.net"
echo "Cosmos DB:     $COSMOS"
echo "Storage acct:  $STORAGE"
echo "Key Vault:     $KV"
```

Static Web App gets created in Phase 3 because it needs your GitHub repo URL.

---

## 6. Phase 2 — Backend (Azure Functions, Python v2 model)

We will build a single `function_app.py` with these HTTP routes:

| Method | Route | Purpose |
|---|---|---|
| POST   | `/api/media`         | Upload a media file + metadata (multipart) |
| GET    | `/api/media`         | List all media (optional `?userId=` filter) |
| GET    | `/api/media/{id}`    | Get one media item |
| PUT    | `/api/media/{id}`    | Update metadata (title, description, tags) |
| DELETE | `/api/media/{id}`    | Delete metadata + blob |
| POST   | `/api/users`         | Register user |
| POST   | `/api/auth/login`    | Login (returns token) |

**Design notes:**
- Use `DefaultAzureCredential` — no connection strings in code. This is a massive rubric win.
- Cosmos DB: use `azure-cosmos` SDK with `aad_credentials`
- Blob: use `azure-storage-blob` with the same credential
- Return JSON. Use Pydantic-style validation (or `dataclasses`) for clean request bodies.
- Password hashing: `bcrypt` (never store plaintext, even in coursework)
- JWT for auth tokens: `pyjwt`

Ask me in your next message to generate the full `function_app.py`, `requirements.txt`, and `host.json`.

---

## 7. Phase 3 — Frontend (Static Web App)

Static HTML/CSS/JS (no framework — keeps marking easy and avoids build complexity). Pages matching your CW1 wireframes:

- `index.html` — gallery (calls `GET /api/media`)
- `upload.html` — upload form (calls `POST /api/media`)
- `my-media.html` — user's own media
- `login.html` / `register.html`
- `media-details.html` — view + edit + delete

Static Web Apps supports a linked Functions API (the `/api/*` routes), so you call the API with a relative path and auth headers flow automatically.

Ask me to generate the HTML/CSS/JS once Phase 2 is done.

---

## 8. Phase 4 — Logic Apps workflow (earns Advanced Features marks)

The workflow:
1. **Trigger:** Event Grid → blob created in `media/` container
2. **Action 1:** Call a Function `/api/internal/moderate` that runs Azure AI Content Safety on the image
3. **Action 2:** If safe, update the Cosmos DB media doc `status = "approved"`
4. **Action 3:** If unsafe, update `status = "rejected"` and delete the blob
5. **Action 4:** Send email via Office 365 Outlook connector (or log to a Teams channel if you have it)

This is built in the Logic App Designer in the portal. You get to show the visual flow on the video — very demo-friendly.

---

## 9. Phase 5 — Advanced features (the 20% you must nail)

Pick **at least 5** of these. The more you implement and show on video, the higher the mark:

1. **Managed Identity** for Functions → Cosmos + Blob (no keys in code) ← Phase 1 already does this
2. **Key Vault** for any remaining secrets (JWT signing key, SMTP password) ← Phase 1 already does this
3. **Application Insights alerts** — create a rule: if failure rate > 5% over 5 min → email
4. **Autoscale rules** — visible in the Functions app scale settings; demonstrate on video
5. **Azure AI Content Safety** on uploaded images (via Logic App or directly in Functions)
6. **Event Grid** triggering the Logic App on blob upload ← Phase 4
7. **Custom domain + HTTPS** on the Static Web App (free Azure cert)
8. **Rate limiting** via APIM (ambitious — skip unless you have time)
9. **Front Door / CDN** in front of the SWA (SWA already has edge by default; configure CDN for blob content instead)
10. **Health check endpoint** + liveness probe

Demonstrate each on video with the portal visible.

---

## 10. Phase 6 — CI/CD with GitHub Actions

Two workflows, both auto-generated the first time you deploy:

- `.github/workflows/azure-functions-app.yml` — deploys Functions on push to `main/backend/**`
- `.github/workflows/azure-static-web-apps.yml` — deploys frontend on push to `main/frontend/**`

The SWA workflow is generated automatically when you link the SWA to your repo in the portal. The Functions workflow is generated when you use the "Deploy to Azure" button in the VS Code Azure Functions extension (or `func azure functionapp publish`).

Commit the workflow files to your repo. **Show a commit → green check in the video.** Marker loves a live deploy.

---

## 11. Phase 7 — Testing & verification

Local:
- Run functions locally: `func start` inside the backend folder
- Test with Postman / `curl` / the VS Code REST Client extension
- Test the SWA locally: `swa start`

Deployed:
- Hit the Function URL directly for each CRUD op and record responses
- Upload a real image through the UI, confirm it lands in Blob container, metadata in Cosmos, Logic App fires
- Force a failure (e.g., invalid Cosmos container name) to confirm App Insights captures it and the alert fires

Keep a **test log** — screenshots of each passing test. Helpful if anything breaks on demo day.

---

## 12. Phase 8 — Video recording (10% of marks)

**Hard cap: 5:00 min. 30-60s over = 10% penalty. Over 1 min = 20% penalty.**

Suggested structure:

| Time | Section | What to show |
|---|---|---|
| 0:00–0:20 | Intro | Face on camera. "I'm Meharjot, B00963621, this is CloudStream." One-sentence pitch. |
| 0:20–1:30 | App walkthrough | Register → login → upload an image → edit title → delete. Show it works. |
| 1:30–2:30 | Azure resources | Portal tour: Resource Group → Function App → Cosmos containers with docs → Blob with files → App Insights live metrics |
| 2:30–3:30 | Logic App + advanced features | Show Event Grid → Logic App Designer visual flow → run history → alert rule fired → managed identity in IAM blade |
| 3:30–4:15 | CI/CD | Show GitHub repo → make a small commit → Actions run → deployment succeeds in Azure |
| 4:15–4:45 | API URIs | Show the Function URL in Postman hitting each CRUD endpoint |
| 4:45–5:00 | Wrap | "Advanced features used: managed identity, Key Vault, Event Grid, Content Safety, alerts, autoscale." Done. |

**Do not repeat CW1 content** (the brief says so). No architecture slides. Pure demo.

**Recording tips:**
- 1920×1080, single monitor only (no dual-screen confusion)
- Close Slack/email/notifications
- Rehearse once fully before recording
- Panopto Capture as required by the brief

---

## 13. Submission checklist

- [ ] Video recorded, under 5:00, uploaded to Panopto
- [ ] GitHub repo public (or shared with marker)
- [ ] Face visible in video
- [ ] All Azure resources visible in video
- [ ] At least 4 advanced features demonstrated
- [ ] CI/CD live-deploy shown
- [ ] CRUD operations demonstrated via both UI and API

---

## Appendix — common pitfalls

- **Cosmos free tier fails to enable** → already used it on this subscription. Drop `--enable-free-tier true` and use 400 RU/s throughput. Still cheap.
- **Storage name already taken** → change `$SUFFIX`.
- **Function cold start slow** → first call after idle can take 5-10s. Warm it up before recording by hitting the health endpoint.
- **CORS errors from SWA → Function** → when using linked API inside SWA this is automatic. If calling a standalone Function App, add CORS in the portal.
- **Managed identity 401** → role assignment can take ~5 min to propagate. Wait and retry before debugging.
- **App Insights shows no data** → confirm `APPLICATIONINSIGHTS_CONNECTION_STRING` app setting exists on the Function App.
