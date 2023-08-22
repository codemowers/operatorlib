rm -fv nuke-all.sh
cat << EOF > argocd-applications.yaml
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  namespace: argocd
  name: codemowers.cloud
spec:
  clusterResourceWhitelist:
    - group: '*'
      kind: '*'
  destinations:
    - namespace: '*'
      server: '*'
  sourceRepos:
    - '*'
EOF
for i in secret-claim redis keydb minio-bucket mysql-database postgres-database wildduck; do
    j=$i-operator
    echo "Updating $j"
    p=samples/$j/templates
    mkdir -p $p
    python3 samples/$j/$(echo $j | tr "-" "_").py nuke >> nuke-all.sh
    python3 samples/$j/$(echo $j | tr "-" "_").py generate-crds > $p/$j-crds.yaml
    python3 samples/$j/$(echo $j | tr "-" "_").py generate-rbac > $p/$j-rbac.yaml
    python3 samples/$j/$(echo $j | tr "-" "_").py generate-deployment --image docker.io/codemowers/$j:latest > $p/$j-deployment.yaml
    ns=$(cat $p/$j-rbac.yaml  | grep namespace | head -n1 | cut -d ":" -f 2 | xargs)
    cat << EOF >> argocd-applications.yaml
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  namespace: argocd
  name: $j
spec:
  destination:
    namespace: $ns
    server: https://kubernetes.default.svc
  project: codemowers.cloud
  source:
    path: samples/$j
    repoURL: https://github.com/codemowers/operatorlib
    targetRevision: HEAD
  syncPolicy: {}
EOF
    cat << EOF > skaffold-$j.yaml
---
apiVersion: skaffold/v3
kind: Config
metadata:
  name: $j
build:
  artifacts:
    - docker:
        dockerfile: samples/$j/Dockerfile
      image: docker.io/codemowers/$j
deploy:
  kubectl:
    flags:
      global:
        - --namespace=$ns
manifests:
  rawYaml:
    - $p/$j-deployment.yaml
EOF
    cat << EOF > samples/$j/Chart.yaml
apiVersion: v2
name: $j
description: Helm chart for $j
type: application
version: 0.1.0
appVersion: 0.0.1
EOF
done
echo "Merging skaffold configs"
cat skaffold-* > skaffold.yaml


cat samples/*/test/* > test.yaml
# TODO: Cleaner way to disable CRD generation
rm -fv samples/wildduck-operator/templates/wildduck-operator-deployment.yaml
rm -fv samples/wildduck-operator/templates/wildduck-operator-crds.yaml
