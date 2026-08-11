# CloudStream

A cloud-native multimedia sharing platform built on Microsoft Azure. Users can register, upload media files, and manage their content through a REST API backed by Flask on Azure App Service, NoSQL storage, and Blob Storage.

Originally built as a serverless Azure Functions API. After the Linux Consumption plan failed to start across all available Azure for Students regions (persistent HTTP 503s — a platform-level limitation, not a code issue), the backend was migrated to Flask on Azure App Service with zero changes to the API contract. `function_app.py` is preserved in the repo as a record of that pivot.

---

## Features

- **Media upload & management** — Upload, list, update, and soft-delete media files via a REST API
- - **User authentication** — Register and login with bcrypt-hashed passwords; all protected routes use JWT Bearer tokens
  - - **Keyless Azure auth** — `DefaultAzureCredential` with managed identity means no secrets are stored in application code
    - - **Observability** — Application Insights tracks requests, exceptions, and custom metrics
      - - **Secret management** — All connection strings and keys stored in Azure Key Vault; fetched at runtime via RBAC
        - - **CI/CD** — GitHub Actions deploys the API and frontend automatically on push to `main`
          - - **One-command provisioning** — `provision.sh` creates and wires all Azure resources from scratch
           
            - ---

            ## Architecture

            ```
            Browser / Client
                  │
                  ▼
            Azure Blob Storage ($web static site — frontend/)
                  │  REST calls
                  ▼
            Azure App Service  ──────────────────────┐
              Flask API (app.py)                     │
                  │                                  │
                  ├── Azure Blob Storage ────── media / thumbnails containers
                  │
                  ├── Azure Cosmos DB ─────── users container
                  │                    └───── media container
                  │
                  ├── Azure Key Vault ─────── connection strings & secrets
                  │
                  └── Application Insights ── request traces & alerts
            ```

            ---

            ## Tech Stack

            | Layer | Technology |
            |---|---|
            | API | Python 3.11, Flask, Gunicorn |
            | Hosting | Azure App Service |
            | Storage | Azure Blob Storage |
            | Database | Azure Cosmos DB (NoSQL) |
            | Auth | JWT (PyJWT), bcrypt, DefaultAzureCredential |
            | Secrets | Azure Key Vault |
            | Monitoring | Azure Application Insights |
            | Frontend | Vanilla HTML / CSS / JavaScript |
            | Frontend hosting | Azure Blob Storage (static website hosting) |
            | CI/CD | GitHub Actions |
            | Provisioning | Azure CLI bash script (`provision.sh`) |

            ---

            ## API Reference

            All endpoints are JSON. Protected routes require `Authorization: Bearer <token>`.

            | Method | Endpoint | Auth | Description |
            |---|---|---|---|
            | GET | `/health` | — | Service status and timestamp |
            | POST | `/users/register` | — | Create account; returns user + JWT |
            | POST | `/auth/login` | — | Authenticate; returns user + JWT |
            | POST | `/media` | ✓ | Upload file (base64) with title, description, tags |
            | GET | `/media` | — | List active media; filter with `?userId=` |
            | GET | `/media/{id}` | — | Get single media document |
            | PUT | `/media/{id}` | ✓ owner | Update title, description, or tags |
            | DELETE | `/media/{id}` | ✓ owner | Soft-delete and remove blob |

            ### Register

            ```json
            POST /users/register
            {
              "email": "user@example.com",
              "password": "password123",
              "displayName": "Meharjot"
            }
            ```

            ### Upload media

            ```json
            POST /media
            Authorization: Bearer <token>
            {
              "title": "My video",
              "description": "A short clip",
              "mediaType": "video/mp4",
              "file": "<base64-encoded content>",
              "contentType": "video/mp4",
              "tags": ["travel", "2025"]
            }
            ```

            ---

            ## Getting Started

            ### Prerequisites

            - Python 3.11+
            - - Azure CLI (`az login` and an active subscription)
              - - Azure Functions Core Tools v4 (optional, for local Functions dev)
               
                - ### 1. Clone and install
               
                - ```bash
                  git clone https://github.com/meharjots/CloudStream.git
                  cd CloudStream
                  pip install -r requirements.txt
                  ```

                  ### 2. Provision Azure resources

                  ```bash
                  chmod +x provision.sh
                  ./provision.sh
                  ```

                  This creates the resource group, storage account (with `media` and `thumbnails` blob containers), Cosmos DB account and database, Application Insights, Function App with managed identity, and Key Vault — and wires RBAC permissions between them.

                  ### 3. Run the API locally

                  ```bash
                  flask --app app.py run
                  ```

                  The API is available at `http://localhost:5000`.

                  ### 4. Deploy

                  Push to `main` — GitHub Actions handles deployment to Azure App Service and Blob Storage static hosting automatically.

                  ---

                  ## Project Structure

                  ```
                  CloudStream/
                  ├── app.py                  # Flask API (primary backend)
                  ├── function_app.py         # Original Azure Functions implementation (archived)
                  ├── host.json               # Azure Functions host config
                  ├── provision.sh            # One-command Azure infrastructure provisioning
                  ├── requirements.txt
                  ├── frontend/
                  │   ├── index.html          # Single-page frontend (vanilla JS)
                  │   └── staticwebapp.config.json
                  ├── .github/
                  │   └── workflows/          # GitHub Actions CI/CD pipelines
                  └── .azure/                 # Azure deployment config
                  ```

                  ---

                  ## Azure Resources

                  | Resource | Purpose |
                  |---|---|
                  | `cloudstream-rg` | Resource group |
                  | `cloudstreamst{suffix}` | Storage account (blobs) |
                  | `cloudstream-cosmos-{suffix}` | Cosmos DB — `users` and `media` containers |
                  | `cloudstream-insights` | Application Insights |
                  | `cloudstream-api-{suffix}` | App Service / Function App |
                  | `cloudstream-kv-{suffix}` | Key Vault |
