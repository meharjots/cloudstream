#!/usr/bin/env bash
# CloudStream CW2 — Azure provisioning script
# Student: Meharjot Singh (B00963621)
# Run section by section the first time. Do NOT `bash provision.sh` until you've done it once successfully.

set -e   # stop on any error

# ====================================================================
# VARIABLES — edit SUFFIX if you get "name already taken" errors
# ====================================================================
SUFFIX=ms963621
RG=cloudstream-rg
LOC=uksouth

STORAGE=cloudstreamst$SUFFIX
COSMOS=cloudstream-cosmos-$SUFFIX
FUNCAPP=cloudstream-api-$SUFFIX
AI=cloudstream-insights
KV=cloudstream-kv-$SUFFIX

echo "Resource group:  $RG"
echo "Region:          $LOC"
echo "Storage:         $STORAGE"
echo "Cosmos:          $COSMOS"
echo "Function App:    $FUNCAPP"
echo "App Insights:    $AI"
echo "Key Vault:       $KV"
echo ""

# ====================================================================
# SECTION 1 — Resource group
# ====================================================================
echo "==> [1/8] Creating resource group..."
az group create -n $RG -l $LOC

# ====================================================================
# SECTION 2 — Storage account (hosts blobs + Functions runtime files)
# ====================================================================
echo "==> [2/8] Creating storage account..."
az storage account create \
  -n $STORAGE -g $RG -l $LOC \
  --sku Standard_LRS \
  --allow-blob-public-access true \
  --min-tls-version TLS1_2

# ====================================================================
# SECTION 3 — Blob containers + CORS
# ====================================================================
echo "==> [3/8] Creating blob containers..."
STORAGE_CONN=$(az storage account show-connection-string -n $STORAGE -g $RG --query connectionString -o tsv)

az storage container create --name media --connection-string "$STORAGE_CONN" --public-access blob
az storage container create --name thumbnails --connection-string "$STORAGE_CONN" --public-access blob

echo "==> Enabling CORS on blob service..."
az storage cors add --services b --methods GET POST PUT DELETE OPTIONS \
  --origins '*' --allowed-headers '*' --exposed-headers '*' --max-age 3600 \
  --connection-string "$STORAGE_CONN"

# ====================================================================
# SECTION 4 — Cosmos DB (free tier: ONE per subscription!)
# If this section fails because you've used the free tier elsewhere,
# remove the --enable-free-tier flag and re-run just this section.
# ====================================================================
echo "==> [4/8] Creating Cosmos DB account (free tier)..."
az cosmosdb create \
  -n $COSMOS -g $RG \
  --kind GlobalDocumentDB \
  --enable-free-tier true \
  --default-consistency-level Session \
  --locations regionName=$LOC failoverPriority=0

echo "==> Creating database and containers..."
az cosmosdb sql database create -a $COSMOS -g $RG -n CloudStreamDB \
  --throughput 1000

az cosmosdb sql container create -a $COSMOS -g $RG -d CloudStreamDB \
  -n users --partition-key-path "/userId"

az cosmosdb sql container create -a $COSMOS -g $RG -d CloudStreamDB \
  -n media --partition-key-path "/userId"

# ====================================================================
# SECTION 5 — Application Insights
# ====================================================================
echo "==> [5/8] Creating Application Insights..."
az extension add --name application-insights --yes 2>/dev/null || true
az monitor app-insights component create --app $AI -g $RG -l $LOC --kind web

# ====================================================================
# SECTION 6 — Function App with system-assigned managed identity
# ====================================================================
echo "==> [6/8] Creating Function App (Python 3.13, Consumption)..."
az functionapp create \
  -n $FUNCAPP -g $RG \
  --storage-account $STORAGE \
  --consumption-plan-location $LOC \
  --runtime python --runtime-version 3.13 \
  --functions-version 4 \
  --os-type Linux \
  --app-insights $AI \
  --assign-identity

# ====================================================================
# SECTION 7 — Key Vault + permissions
# ====================================================================
echo "==> [7/8] Creating Key Vault..."
az keyvault create -n $KV -g $RG -l $LOC --enable-rbac-authorization true

echo "==> Granting YOU Key Vault Secrets Officer role..."
ME=$(az ad signed-in-user show --query id -o tsv)
KV_ID=$(az keyvault show -n $KV -g $RG --query id -o tsv)
az role assignment create \
  --assignee $ME \
  --role "Key Vault Secrets Officer" \
  --scope $KV_ID

echo "==> Granting Function App managed identity Key Vault Secrets User role..."
FUNC_PRINCIPAL=$(az functionapp identity show -n $FUNCAPP -g $RG --query principalId -o tsv)
az role assignment create \
  --assignee $FUNC_PRINCIPAL \
  --role "Key Vault Secrets User" \
  --scope $KV_ID

# ====================================================================
# SECTION 8 — Data-plane access for Function managed identity
# ====================================================================
echo "==> [8/8] Granting Function managed identity data-plane access..."

echo "  - Cosmos DB Built-in Data Contributor..."
COSMOS_ID=$(az cosmosdb show -n $COSMOS -g $RG --query id -o tsv)
az cosmosdb sql role assignment create \
  --account-name $COSMOS -g $RG \
  --scope "$COSMOS_ID" \
  --principal-id $FUNC_PRINCIPAL \
  --role-definition-id 00000000-0000-0000-0000-000000000002

echo "  - Storage Blob Data Contributor..."
STORAGE_ID=$(az storage account show -n $STORAGE -g $RG --query id -o tsv)
az role assignment create \
  --assignee $FUNC_PRINCIPAL \
  --role "Storage Blob Data Contributor" \
  --scope $STORAGE_ID

echo ""
echo "===================================="
echo "  Provisioning complete."
echo "===================================="
echo "Function App URL:  https://$FUNCAPP.azurewebsites.net"
echo "Cosmos DB:         $COSMOS"
echo "Storage account:   $STORAGE"
echo "Key Vault:         $KV"
echo ""
echo "Save these values — you'll need them in Phase 2."
