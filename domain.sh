#!/usr/bin/env bash
# Put pos.gencbeauty.com in front of the Cloud Run service with a global
# external HTTPS load balancer and a Google-managed certificate.
#
# Why a load balancer rather than a Cloud Run domain mapping: a mapping needs
# the project to prove ownership of gencbeauty.com in Search Console, which
# only the domain owner can do. The load balancer needs nothing from the
# owner except one DNS record pointing at the static IP this script reserves.
# Cost: roughly US$18-20 a month for the forwarding rules.
#
# Run once, then send the DNS record it prints to the domain owner. The
# certificate stays PROVISIONING until the record resolves, then goes ACTIVE
# within about an hour. Re-run safely: every step skips what already exists.
set -euo pipefail
PROJECT="${PROJECT:-genc-pos-2026}"
REGION="${REGION:-northamerica-northeast1}"
SERVICE="${SERVICE:-genc-pos}"
DOMAIN="${DOMAIN:-pos.gencbeauty.com}"
N=genc-pos
g() { gcloud --project="$PROJECT" --quiet "$@"; }
have() { g compute "$1" describe "$2" "${@:3}" >/dev/null 2>&1; }

g services enable compute.googleapis.com
have addresses $N-ip --global || g compute addresses create $N-ip --global
have network-endpoint-groups $N-neg --region="$REGION" \
  || g compute network-endpoint-groups create $N-neg --region="$REGION" --network-endpoint-type=serverless --cloud-run-service="$SERVICE"
have backend-services $N-backend --global \
  || { g compute backend-services create $N-backend --global --load-balancing-scheme=EXTERNAL_MANAGED --protocol=HTTPS
       g compute backend-services add-backend $N-backend --global --network-endpoint-group=$N-neg --network-endpoint-group-region="$REGION"; }
have url-maps $N-urlmap --global || g compute url-maps create $N-urlmap --default-service=$N-backend --global
have ssl-certificates $N-cert --global || g compute ssl-certificates create $N-cert --domains="$DOMAIN" --global
have target-https-proxies $N-https --global || g compute target-https-proxies create $N-https --url-map=$N-urlmap --ssl-certificates=$N-cert --global
have forwarding-rules $N-https-rule --global \
  || g compute forwarding-rules create $N-https-rule --global --address=$N-ip --target-https-proxy=$N-https --ports=443 --load-balancing-scheme=EXTERNAL_MANAGED

# http:// -> https:// redirect
if ! have url-maps $N-redirect --global; then
  tmp=$(mktemp)
  printf 'name: %s-redirect\ndefaultUrlRedirect:\n  httpsRedirect: true\n  redirectResponseCode: MOVED_PERMANENTLY_DEFAULT\n' "$N" > "$tmp"
  g compute url-maps import $N-redirect --global --source="$tmp"
  rm -f "$tmp"
fi
have target-http-proxies $N-http --global || g compute target-http-proxies create $N-http --url-map=$N-redirect --global
have forwarding-rules $N-http-rule --global \
  || g compute forwarding-rules create $N-http-rule --global --address=$N-ip --target-http-proxy=$N-http --ports=80 --load-balancing-scheme=EXTERNAL_MANAGED

IP=$(g compute addresses describe $N-ip --global --format='value(address)')
echo
echo "DNS record for the owner of gencbeauty.com:"
echo "  Type A   Name pos   Value $IP   TTL 300"
echo
echo "Certificate status (ACTIVE once DNS resolves):"
g compute ssl-certificates describe $N-cert --global --format='value(managed.status,managed.domainStatus)'
