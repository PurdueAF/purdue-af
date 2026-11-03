# af-x509-secrets

Keeps the facility's grid proxy, the Secret `af-x509-proxy`, renewed from the
facility robot certificate in `grid-certs-secret`. Each run builds a proxy and
checks it before updating the Secret in place, so a failed run leaves the last
proxy to serve until it expires. `AFGridProxyNotRenewed` fires when renewal
stops.

`grid-certs-secret` lives only on the cluster. Create it from the certificate
files, on each cluster that runs this CronJob:

```bash
kubectl -n cms create secret generic grid-certs-secret \
  --from-file=usercert.pem --from-file=userkey.pem --from-file=passphrase
```

Show when the certificate expires:

```bash
kubectl -n cms get secret grid-certs-secret -o jsonpath='{.data.usercert\.pem}' \
  | base64 -d | openssl x509 -noout -enddate
```

The renewal reads all three keys, and the ServiceX releases mount the same
Secret in their own renewal jobs and upgrade hooks.
