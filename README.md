# operatorlib

This is Python library for building Kubernetes operators.
It addresses recurring issues in Kubernetes operator development such as
CRD, RBAC and deployment manifest generation,
stateful set instantiation,
PVC resizing,
secret generation and much more.

Refer to `samples/` directory to see how to use the library.

# Developing

Run `update-manifests.sh` to update Helm chart templates and Skaffold configs.

Before running Skaffold make sure dependencies are installed,
refer to each sample operator README.

To deploy and run:

```
find samples/ -name "*-crds.yaml" -exec kubectl apply -f {} \;
find samples/ -name "*-rbac.yaml" -exec kubectl apply -f {} \;
find samples/ -name "*-class.yaml" -exec kubectl apply -f {} \;
skaffold dev --build-concurrency=0
```

To wipe status conditions array:

```
for j in $(kubectl get redises.codemowers.cloud -o name); do
  kubectl patch --type=merge --subresource=status $j -p '{"status":{"conditions":[]}}';
done
```
