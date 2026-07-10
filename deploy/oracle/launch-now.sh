#!/usr/bin/env bash
set -uo pipefail
export SUPPRESS_LABEL_WARNING=True
export OCI_CLI_SUPPRESS_FILE_ENCODING_WARNING=True

REGION=eu-madrid-1
COMPARTMENT=ocid1.tenancy.oc1..aaaaaaaa6f6y2uw2fdcfdgsykf3z6tt6weky5c7n3ylra3brkc6sotvgakla
AD=dKyg:EU-MADRID-1-AD-1
VCN=ocid1.vcn.oc1.eu-madrid-1.amaaaaaa7jfzhlqauzpuair2h25zej4u7skdhejyb4rob77vt7ezvlzmoz5q
SUBNET=ocid1.subnet.oc1.eu-madrid-1.aaaaaaaabmv2s2qqzwblmtaeuutlwvzrimrjlt5mct5sbl7h4fvpi7jpyhoq
IMAGE_A1=ocid1.image.oc1.eu-madrid-1.aaaaaaaaghkhyje66vjl4gaq3m4kepw435yhbv2zhjjy5t5g3ykc6l2ftmea
IMAGE_E2=ocid1.image.oc1.eu-madrid-1.aaaaaaaa3yrk55rmvir2gn3xwa7owlxkbnhji3ivxvhohyb6o35mpisaxobq
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

RT=$(oci network vcn get --vcn-id "$VCN" --region "$REGION" --query 'data."default-route-table-id"' --raw-output)
SL=$(oci network vcn get --vcn-id "$VCN" --region "$REGION" --query 'data."default-security-list-id"' --raw-output)
IGW=$(oci network internet-gateway list --compartment-id "$COMPARTMENT" --vcn-id "$VCN" --region "$REGION" --query 'data[0].id' --raw-output)
if [ -z "$IGW" ] || [ "$IGW" = "null" ]; then
  IGW=$(oci network internet-gateway create --compartment-id "$COMPARTMENT" --vcn-id "$VCN" --is-enabled true --display-name ainvestor-igw --wait-for-state AVAILABLE --region "$REGION" --query 'data.id' --raw-output)
fi

oci network route-table update --rt-id "$RT" \
  --route-rules "[{\"cidrBlock\":\"0.0.0.0/0\",\"networkEntityId\":\"$IGW\"}]" \
  --force --region "$REGION" >/dev/null
oci network security-list update --security-list-id "$SL" \
  --ingress-security-rules '[{"protocol":"6","source":"0.0.0.0/0","tcpOptions":{"destinationPortRange":{"max":22,"min":22}}},{"protocol":"6","source":"0.0.0.0/0","tcpOptions":{"destinationPortRange":{"max":8000,"min":8000}}}]' \
  --force --region "$REGION" >/dev/null

echo "=== Intentando VM.Standard.A1.Flex ==="
if oci compute instance launch \
  --availability-domain "$AD" \
  --compartment-id "$COMPARTMENT" \
  --display-name ainvestor \
  --image-id "$IMAGE_A1" \
  --shape VM.Standard.A1.Flex \
  --shape-config '{"ocpus":1,"memoryInGBs":6}' \
  --subnet-id "$SUBNET" \
  --assign-public-ip true \
  --metadata '{"ssh_authorized_keys":"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEqOeeuYIyV4p8toj980A0gjtw8e+Gou/6Bz/aAbfug7 ainvestor-oracle"}' \
  --region "$REGION" \
  --wait-for-state RUNNING \
  --max-wait-seconds 600; then
  INST=$(oci compute instance list --compartment-id "$COMPARTMENT" --display-name ainvestor --region "$REGION" --query 'data[0].id' --raw-output)
  IP=$(oci compute instance list-vnics --instance-id "$INST" --region "$REGION" --query 'data[0]."public-ip"' --raw-output)
  echo "SUCCESS IP=$IP"
  exit 0
fi

echo "=== Intentando VM.Standard.E2.1.Micro ==="
if oci compute instance launch \
  --availability-domain "$AD" \
  --compartment-id "$COMPARTMENT" \
  --display-name ainvestor \
  --image-id "$IMAGE_E2" \
  --shape VM.Standard.E2.1.Micro \
  --subnet-id "$SUBNET" \
  --assign-public-ip true \
  --metadata '{"ssh_authorized_keys":"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEqOeeuYIyV4p8toj980A0gjtw8e+Gou/6Bz/aAbfug7 ainvestor-oracle"}' \
  --region "$REGION" \
  --wait-for-state RUNNING \
  --max-wait-seconds 600; then
  INST=$(oci compute instance list --compartment-id "$COMPARTMENT" --display-name ainvestor --region "$REGION" --query 'data[0].id' --raw-output)
  IP=$(oci compute instance list-vnics --instance-id "$INST" --region "$REGION" --query 'data[0]."public-ip"' --raw-output)
  echo "SUCCESS IP=$IP"
  exit 0
fi

echo "FALLO: sin capacidad o error de lanzamiento"
exit 1