"""
 Copyright 2023 Google LLC

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      https://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

r"""xpk (Accelerated Processing Kit).

Next Steps:
- Cluster describe is broken by Cacheimage since that counts as a workload.
- Cluster describe: count by jobset.
- If any instance goes down, bring down the whole job.
- How to more gracefully handle job failures, distinguishing between software
  and infra?
- Look into --docker-name and --docker-image.
  Shouldn't one string be adequate to express what we want?
- Apply learnings from about private, region, coredns, etc:
- Enable special preheater
- Make Argparse logic this a function?
  - Obvious logic that starts in main instead of here in code but args will
    not be a universal argument.
"""

import argparse
import datetime
import os
import random
import re
import string
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

################### Compatibility Check ###################
# Check that the user runs the below version or greater.

major_version_supported = 3
minor_version_supported = 10

user_major_version = sys.version_info[0]
user_minor_version = sys.version_info[1]
if (
    user_major_version < major_version_supported
    or user_minor_version < minor_version_supported
):
  raise RuntimeError('xpk must be run with Python'
      f' {major_version_supported}.{minor_version_supported} or greater.'
      f' User currently is running {user_major_version}.{user_minor_version}'
  )


################### Internally used constants ##############

default_docker_image = 'python:3.10'
default_script_dir = os.getcwd()
default_gke_version="1.29.1-gke.1589017"
xpk_current_version = "0.3.0"
_CLUSTER_QUEUE_NAME='cluster-queue'
_LOCAL_QUEUE_NAME='multislice-queue'
h100_device_type = 'h100-80gb-8'
_CLUSTER_RESOURCES_CONFIGMAP = 'resources-configmap'
_CLUSTER_METADATA_CONFIGMAP = 'metadata-configmap'
_XPK_SERVICE_ACCOUNT = 'xpk-sa'
# Set to True to attach a service account to cluster & node pools
_SERVICE_ACCOUNT_FEATURE_FLAG = xpk_current_version >= "0.4.0"


workload_create_yaml = """apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: {args.workload}
  labels:
    kueue.x-k8s.io/queue-name: {local_queue_name}  # Name of the LocalQueue
    xpk.google.com/workload: {args.workload}
  annotations:
    alpha.jobset.sigs.k8s.io/exclusive-topology: cloud.google.com/gke-nodepool # 1:1 job replica to node pool assignment
spec:
  failurePolicy:
    maxRestarts: {args.max_restarts}
  replicatedJobs:
    - name: slice-job
      replicas: {args.num_slices}
      template:
        spec:
          parallelism: {system.vms_per_slice}    # Equal to the number of VMs per slice
          completions: {system.vms_per_slice}    # Same as the above.
          backoffLimit: 0   # When any pod fails, the job is failed
          template:
            metadata:
              labels:
                xpk.google.com/workload: {args.workload}
            spec:
              schedulerName: {args.scheduler}
              restartPolicy: Never
              {affinity}
              nodeSelector:
                {accelerator_label}
                {machine_label}
              priorityClassName: {args.priority}
              hostNetwork: true
              dnsPolicy: ClusterFirstWithHostNet
              terminationGracePeriodSeconds: {args.termination_grace_period_seconds}
              containers:
              {container}
                {env}
                volumeMounts:
                - mountPath: /dev/shm
                  name: dshm-2
              volumes:
              - emptyDir:
                  medium: Memory
                name: dshm-2
"""

gpu_workload_create_yaml = """apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: {args.workload}
  labels:
    kueue.x-k8s.io/queue-name: multislice-queue  # Name of the LocalQueue
    xpk.google.com/workload: {args.workload}
spec:
  failurePolicy:
    maxRestarts: {args.max_restarts}
  replicatedJobs:
    - name: slice-job
      replicas: 1
      template:
        spec:
          parallelism: {args.num_nodes}
          completions: {args.num_nodes}
          backoffLimit: 0   # When any pod fails, the job is failed
          template:
            metadata:
              labels:
                xpk.google.com/workload: {args.workload}
            spec:
              schedulerName: {args.scheduler}
              restartPolicy: Never
              affinity:
                nodeAffinity:
                  requiredDuringSchedulingIgnoredDuringExecution:
                    nodeSelectorTerms:
                    - matchExpressions:
                      - key: cloud.google.com/gke-accelerator
                        operator: Exists
                      - key: cloud.google.com/gke-nodepool
                        operator: In
                        values: [{node_pool_name}]
              nodeSelector:
                {accelerator_label}
                {machine_label}
              priorityClassName: {args.priority}
              hostNetwork: true
              dnsPolicy: ClusterFirstWithHostNet
              terminationGracePeriodSeconds: {args.termination_grace_period_seconds}
              tolerations:
              - operator: "Exists"
                key: nvidia.com/gpu
              volumes:
              - name: nvidia-install-dir-host
                hostPath:
                  path: /home/kubernetes/bin/nvidia/lib64
              - name: tcpd-socket
                hostPath:
                  path: /run/tcpx
              - name: shared-memory
                emptyDir:
                  medium: "Memory"
                  sizeLimit: 200Gi
              - name: workload-terminated-volume
                emptyDir:
              - name: tcpx-nccl-plugin-volume
                emptyDir:
              initContainers:
              - name: tcpx-nccl-plugin-installer
                image: us-docker.pkg.dev/gce-ai-infra/gpudirect-tcpx/nccl-plugin-gpudirecttcpx-dev:v3.1.6_2023_10_06
                imagePullPolicy: Always
                restartPolicy: Always
                volumeMounts:
                - name: tcpx-nccl-plugin-volume
                  mountPath: /var/lib/tcpx
                resources:
                  requests:
                    cpu: 150m
              containers:
              - name: tcpd-daemon
                image: us-docker.pkg.dev/gce-ai-infra/gpudirect-tcpx/tcpgpudmarxd-dev:v2.0.9
                imagePullPolicy: Always
                command:
                - "bash"
                - "-c"
                - |
                  /tcpgpudmarxd/build/app/tcpgpudmarxd --gpu_nic_preset a3vm --gpu_shmem_type fd --setup_param "--verbose 128 2 0" &
                  while [ ! -e "/usr/share/workload/workload_terminated" ]; do sleep 10; echo "sleeping"; done
                securityContext:
                  privileged: true
                volumeMounts:
                - name: nvidia-install-dir-host
                  mountPath: /usr/local/nvidia/lib64
                - name: tcpd-socket
                  mountPath: /tmp
                - name: workload-terminated-volume
                  mountPath: /usr/share/workload
                env:
                - name: LD_LIBRARY_PATH
                  value: /usr/local/nvidia/lib64
              - name: maxtext-tcpx
                image: {docker_image}
                imagePullPolicy: Always
                securityContext:
                  privileged: true
                ports:
                    - containerPort: 6002
                env:
                  - name: REPLICATED_JOB_NAME
                    valueFrom:
                      fieldRef:
                        fieldPath: metadata.annotations['jobset.sigs.k8s.io/replicatedjob-name']
                  - name: JOBSET_NAME
                    valueFrom:
                      fieldRef:
                        fieldPath: metadata.annotations['jobset.sigs.k8s.io/jobset-name']
                  - name: JAX_COORDINATOR_ADDRESS
                    value: "$(JOBSET_NAME)-$(REPLICATED_JOB_NAME)-0-0.$(JOBSET_NAME)"
                  - name: NNODES
                    value: "{args.num_nodes}"
                  - name: NODE_RANK
                    valueFrom:
                      fieldRef:
                        fieldPath: metadata.annotations['batch.kubernetes.io/job-completion-index']
                  - name: USE_GPUDIRECT
                    value: "tcpx"
                  - name: GPUS_PER_NODE
                    value: "{chips_per_vm}"
                  - name: JAX_COORDINATOR_PORT
                    value: "6002"
                  - name: LD_LIBRARY_PATH
                    value: /usr/local/nvidia/lib64
                  - name: COMMAND
                    value: "{command}"
                  {args.env}
                command:
                  - "bash"
                  - "-c"
                  - |
                    echo XPK Start: $(date) ; _sigterm() ( kill -SIGTERM $!;); trap _sigterm SIGTERM; (cd /deps && bash gpu_multi_process_run.sh) & PID=$!; while kill -0 $PID 2>/dev/null; do sleep 5; done; EXIT_CODE=$? ; echo XPK End: $(date); echo EXIT_CODE=$EXIT_CODE; echo Main app is done > /usr/share/workload/workload_terminated
                volumeMounts:
                  - name: nvidia-install-dir-host
                    mountPath: /usr/local/nvidia/lib64
                  - name: tcpx-nccl-plugin-volume
                    mountPath: /usr/local/tcpx
                  - name: tcpd-socket
                    mountPath: /tmp
                  - name: shared-memory
                    mountPath: /dev/shm
                  - name: workload-terminated-volume
                    mountPath: /usr/share/workload
                resources:
                  limits:
                    nvidia.com/gpu: {chips_per_vm}
"""

pw_workload_create_yaml = """apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: {args.workload}
  labels:
    kueue.x-k8s.io/queue-name: {local_queue_name}  # Name of the LocalQueue
    xpk.google.com/workload: {args.workload}
spec:
  failurePolicy:
    maxRestarts: {args.max_restarts}
  successPolicy:
    operator: "All"
    targetReplicatedJobs:
    - "main"
  replicatedJobs:
  - name: worker
    replicas: {args.num_slices}
    template:
      metadata:
        annotations:
          alpha.jobset.sigs.k8s.io/exclusive-topology: cloud.google.com/gke-nodepool
        labels:
          xpk.google.com/workload: {args.workload}
      spec:
        backoffLimit: 0
        completions: {system.vms_per_slice}
        parallelism: {system.vms_per_slice}
        template:
          spec:
            containers:
            - args:
              {pathways_worker_args}
              image: {args.server_image}
              imagePullPolicy: Always
              name: pathways-worker
              ports:
              - containerPort: 38677
              - containerPort: 8471
              - containerPort: 8080
              resources:
                limits:
                  {resource_type}: {system.chips_per_vm}
              securityContext:
                privileged: true
              volumeMounts:
              - mountPath: /tmp
                name: shared-tmp
            nodeSelector:
              {accelerator_label}
              {machine_label}
            priorityClassName: {args.priority}
            volumes:
            - hostPath:
                path: /tmp
                type: DirectoryOrCreate
              name: shared-tmp
  - name: rm
    replicas: 1
    template:
      metadata:
        labels:
          xpk.google.com/workload: {args.workload}
      spec:
        backoffLimit: 0
        completions: 1
        parallelism: 1
        template:
          spec:
            containers:
            - args:
              {pathways_rm_args}
              env:
              - name: REPLICATED_JOB_NAME
                valueFrom:
                  fieldRef:
                    fieldPath: metadata.annotations['jobset.sigs.k8s.io/replicatedjob-name']
              - name: JOBSET_NAME
                valueFrom:
                  fieldRef:
                    fieldPath: metadata.annotations['jobset.sigs.k8s.io/jobset-name']
              - name: HOST_ADDRESS
                value: $(JOBSET_NAME)-$(REPLICATED_JOB_NAME)-0-0.$(JOBSET_NAME)
              - name: TPU_SKIP_MDS_QUERY
                value: "true"
              image: {args.server_image}
              imagePullPolicy: Always
              name: pathways-rm
              ports:
              - containerPort: 38677
              resources:
                limits:
                  cpu: "4"
                  memory: 8G
              securityContext:
                privileged: true
              volumeMounts:
              - mountPath: /tmp
                name: shared-tmp
            nodeSelector:
              cloud.google.com/gke-nodepool: cpu-rm-np
            volumes:
            - hostPath:
                path: /tmp
                type: DirectoryOrCreate
              name: shared-tmp
  - name: proxy
    replicas: 1
    template:
      metadata:
        labels:
          xpk.google.com/workload: {args.workload}
      spec:
        backoffLimit: 0
        completions: 1
        parallelism: 1
        template:
          spec:
            containers:
            - args:
              {pathways_proxy_args}
              image: {args.proxy_server_image}
              imagePullPolicy: Always
              name: pathways-proxy
              ports:
              - containerPort: 38676
              resources:
                limits:
                  cpu: "24"
                  memory: 100G
            nodeSelector:
              cloud.google.com/gke-nodepool: cpu-proxy-np
  - name: main
    replicas: 1
    template:
      metadata:
        labels:
          xpk.google.com/workload: {args.workload}
      spec:
        backoffLimit: 0
        completions: 1
        parallelism: 1
        template:
          spec:
            containers:
              {container}
            nodeSelector:
              cloud.google.com/gke-nodepool: cpu-user-np
            restartPolicy: OnFailure
            volumes:
            - hostPath:
                path: /tmp
                type: DirectoryOrCreate
              name: shared-tmp
"""
script_dir_dockerfile = """FROM {base_docker_image}

# Set the working directory in the container
WORKDIR /app

# Copy all files from local workspace into docker container
COPY . .

WORKDIR /app
"""

cluster_set_crd_yaml = """apiVersion: kueue.x-k8s.io/v1beta1
kind: ResourceFlavor
metadata:
  name: {cluster_hardware_name}
spec:
  nodeLabels:
    {accelerator_label}
    {machine_label}
---
{pw_resource_flavors}
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: {cluster_queue_name}
spec:
  preemption:
      reclaimWithinCohort: Never # Don't preempt other queues in the cohort.
      withinClusterQueue: LowerPriority
  namespaceSelector: {{}} # match all.
  resourceGroups:
  {covered_resources_config}
  {pw_resources_kueue}
---
apiVersion: kueue.x-k8s.io/v1beta1
kind: LocalQueue
metadata:
  namespace: default
  name: {local_queue_name}
spec:
  clusterQueue: {cluster_queue_name}
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: very-low
value: 100
globalDefault: false
description: "Very Low"
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: low
value: 250
globalDefault: false
description: "Low"
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: medium
value: 500
globalDefault: false
description: "Medium"
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: high
value: 750
globalDefault: false
description: "High"
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: very-high
value: 1000
globalDefault: false
description: "Very High"
"""

cluster_preheat_yml = """
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: {cachekey}
  labels:
    k8s-app: {cachekey}
spec:
  selector:
    matchLabels:
      k8s-app: {cachekey}
  updateStrategy:
    type: RollingUpdate
  template:
    metadata:
      labels:
        name: {cachekey}
        k8s-app: {cachekey}
    spec:
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: {nodeSelectorKey}
                operator: Exists
      tolerations:
      - operator: "Exists"
      containers:
      - image: {image_name}
        name: {cachekey}
        command: [ "sleep", "inf" ]
"""

cluster_configmap_yaml = """kind: ConfigMap
apiVersion: v1
metadata:
  name: {name}
data:
  {data}
"""

# cluster_network_yaml: the config when creating the network for a3 cluster
cluster_network_yaml = """
apiVersion: networking.gke.io/v1
kind: Network
metadata:
  name: vpc1
spec:
  parametersRef:
    group: networking.gke.io
    kind: GKENetworkParamSet
    name: vpc1
  type: Device
---
apiVersion: networking.gke.io/v1
kind: Network
metadata:
  name: vpc2
spec:
  parametersRef:
    group: networking.gke.io
    kind: GKENetworkParamSet
    name: vpc2
  type: Device
---
apiVersion: networking.gke.io/v1
kind: Network
metadata:
  name: vpc3
spec:
  parametersRef:
    group: networking.gke.io
    kind: GKENetworkParamSet
    name: vpc3
  type: Device
---
apiVersion: networking.gke.io/v1
kind: Network
metadata:
  name: vpc4
spec:
  parametersRef:
    group: networking.gke.io
    kind: GKENetworkParamSet
    name: vpc4
  type: Device
---
apiVersion: networking.gke.io/v1
kind: GKENetworkParamSet
metadata:
  name: vpc1
spec:
  vpc: {cluster_name}-net-1
  vpcSubnet: {cluster_name}-sub-1
  deviceMode: NetDevice
---
apiVersion: networking.gke.io/v1
kind: GKENetworkParamSet
metadata:
  name: vpc2
spec:
  vpc: {cluster_name}-net-2
  vpcSubnet: {cluster_name}-sub-2
  deviceMode: NetDevice
---
apiVersion: networking.gke.io/v1
kind: GKENetworkParamSet
metadata:
  name: vpc3
spec:
  vpc: {cluster_name}-net-3
  vpcSubnet: {cluster_name}-sub-3
  deviceMode: NetDevice
---
apiVersion: networking.gke.io/v1
kind: GKENetworkParamSet
metadata:
  name: vpc4
spec:
  vpc: {cluster_name}-net-4
  vpcSubnet: {cluster_name}-sub-4
  deviceMode: NetDevice
"""

# Add IAM roles to attach to service account used by node pools in the cluster
IAMRoles = {
  'Kubernetes Engine Admin': 'roles/container.admin',
  'Artifact Registry Writer': 'roles/artifactregistry.writer',
  'Monitoring Admin': 'roles/monitoring.admin',
  'Logging Admin': 'roles/logging.admin',
  'Storage Admin': 'roles/storage.admin',
  'Vertex AI Administrator': 'roles/aiplatform.admin'
}


AcceleratorType = {
  'TPU': 1,
  'GPU': 2,
  'CPU': 3
}

@dataclass
class AcceleratorCharacteristics:
  resource_type: str
  accelerator_label: int
  machine_label: str

AcceleratorTypeToAcceleratorCharacteristics = {
     # TPU
      AcceleratorType['TPU']: AcceleratorCharacteristics(
      'google.com/tpu', 'cloud.google.com/gke-tpu-accelerator', 'cloud.google.com/gke-tpu-topology'
      ),
     # GPU
      AcceleratorType['GPU']: AcceleratorCharacteristics(
      'nvidia.com/gpu', 'cloud.google.com/gke-accelerator', 'cloud.google.com/gce-machine-type'
      ),
     # CPU
      AcceleratorType['CPU']: AcceleratorCharacteristics(
      'cpu', '', 'cloud.google.com/gke-nodepool'
      )
}


@dataclass
class SystemCharacteristics:
  topology: str
  vms_per_slice: int
  gke_accelerator: str
  gce_machine_type: str
  chips_per_vm: int
  accelerator_type: AcceleratorType # type: ignore
  device_type: str

################### Subcommand Helper Functions #############
""" !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
IF YOU MODIFY THE BELOW UserFacingNameToSystemCharacteristics MAP YOU SHOULD ALSO ADD CORRESPONDING
MODIFICATIONS TO UserFacingNameToSystemCharacteristics IN MaxText/accelerator_to_spec_map.py !!!!! """
# vvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvv
UserFacingNameToSystemCharacteristics = {
    # GPU system characteristics
    # A100-40gb-$CHIPS
    'a100-40gb-1': SystemCharacteristics(
      'N/A', 1, 'nvidia-tesla-a100', 'a2-highgpu-1g', 1, AcceleratorType['GPU'], 'a100-40gb-1'
    ),
    'a100-40gb-2': SystemCharacteristics(
      'N/A', 1, 'nvidia-tesla-a100', 'a2-highgpu-2g', 2, AcceleratorType['GPU'], 'a100-40gb-2'
    ),
    'a100-40gb-4': SystemCharacteristics(
      'N/A', 1, 'nvidia-tesla-a100', 'a2-highgpu-4g', 4, AcceleratorType['GPU'], 'a100-40gb-4'
    ),
    'a100-40gb-8': SystemCharacteristics(
      'N/A', 1, 'nvidia-tesla-a100', 'a2-highgpu-8g', 8, AcceleratorType['GPU'], 'a100-40gb-8'
    ),
     # H100-80gb-$CHIPS
    'h100-80gb-8': SystemCharacteristics(
      'N/A', 1, 'nvidia-h100-80gb', 'a3-highgpu-8g', 8, AcceleratorType['GPU'], 'h100-80gb-8'
    ),

    # TPU system characteristics
    # v5p
    'v5p-8': SystemCharacteristics(
      '2x2x1', 1, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8'
    ),
    'v5p-16': SystemCharacteristics(
      '2x2x2', 2, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-16'
    ),
    'v5p-32': SystemCharacteristics(
      '2x2x4', 4, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-32'
    ),
    'v5p-64': SystemCharacteristics(
      '2x4x4', 8, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-64'
    ),
    'v5p-128': SystemCharacteristics(
      '4x4x4', 16, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-128'
    ),
    'v5p-256': SystemCharacteristics(
      '4x4x8', 32, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-256'
    ),
    'v5p-384': SystemCharacteristics(
      '4x4x12', 48, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-384'
    ),
    'v5p-512': SystemCharacteristics(
      '4x8x8', 64, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-512'
    ),
    'v5p-640': SystemCharacteristics(
      '4x4x20', 80, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-640'
    ),
    'v5p-768': SystemCharacteristics(
      '4x8x12', 96, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-768'
    ),
    'v5p-896': SystemCharacteristics(
      '4x4x28', 112, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-896'
    ),
    'v5p-1024': SystemCharacteristics(
      '8x8x8', 128, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1024'
    ),
    'v5p-1152': SystemCharacteristics(
      '4x12x12', 144, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1152'
    ),
    'v5p-1280': SystemCharacteristics(
      '4x8x20', 160, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1280'
    ),
    'v5p-1408': SystemCharacteristics(
      '4x4x44', 176, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1408'
    ),
    'v5p-1536': SystemCharacteristics(
      '8x8x12', 192, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1536'
    ),
    'v5p-1664': SystemCharacteristics(
      '4x4x52', 208, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1664'
    ),
    'v5p-1792': SystemCharacteristics(
      '4x8x28', 224, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1792'
    ),
    'v5p-1920': SystemCharacteristics(
      '4x12x20', 240, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1920'
    ),
    'v5p-2048': SystemCharacteristics(
      '8x8x16', 256, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2048'
    ),
    'v5p-2176': SystemCharacteristics(
      '4x4x68', 272, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2176'
    ),
    'v5p-2304': SystemCharacteristics(
      '8x12x12', 288, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2304'
    ),
    'v5p-2432': SystemCharacteristics(
      '4x4x76', 304, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2432'
    ),
    'v5p-2560': SystemCharacteristics(
      '8x8x20', 320, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2560'
    ),
    'v5p-2688': SystemCharacteristics(
      '4x12x28', 336, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2688'
    ),
    'v5p-2816': SystemCharacteristics(
      '4x8x44', 352, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2816'
    ),
    'v5p-2944': SystemCharacteristics(
      '4x4x92', 368, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2944'
    ),
    'v5p-3072': SystemCharacteristics(
      '8x12x16', 384, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3072'
    ),
    'v5p-3200': SystemCharacteristics(
      '4x20x20', 400, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3200'
    ),
    'v5p-3328': SystemCharacteristics(
      '4x8x52', 416, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3328'
    ),
    'v5p-3456': SystemCharacteristics(
      '12x12x12', 432, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3456'
    ),
    'v5p-3584': SystemCharacteristics(
      '8x8x28', 448, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3584'
    ),
    'v5p-3712': SystemCharacteristics(
      '4x4x116', 464, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3712'
    ),
    'v5p-3840': SystemCharacteristics(
      '8x12x20', 480, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3840'
    ),
    'v5p-3968': SystemCharacteristics(
      '4x4x124', 496, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3968'
    ),
    'v5p-4096': SystemCharacteristics(
      '8x16x16', 512, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4096'
    ),
    'v5p-4224': SystemCharacteristics(
      '4x12x44', 528, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4224'
    ),
    'v5p-4352': SystemCharacteristics(
      '4x8x68', 544, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4352'
    ),
    'v5p-4480': SystemCharacteristics(
      '4x20x28', 560, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4480'
    ),
    'v5p-4608': SystemCharacteristics(
      '12x12x16', 576, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4608'
    ),
    'v5p-4736': SystemCharacteristics(
      '4x4x148', 592, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4736'
    ),
    'v5p-4864': SystemCharacteristics(
      '4x8x76', 608, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4864'
    ),
    'v5p-4992': SystemCharacteristics(
      '4x12x52', 624, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4992'
    ),
    'v5p-5120': SystemCharacteristics(
      '8x16x20', 640, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-5120'
    ),
    'v5p-5248': SystemCharacteristics(
      '4x4x164', 656, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-5248'
    ),
    'v5p-5376': SystemCharacteristics(
      '8x12x28', 672, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-5376'
    ),
    'v5p-5504': SystemCharacteristics(
      '4x4x172', 688, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-5504'
    ),
    'v5p-5632': SystemCharacteristics(
      '8x8x44', 704, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-5632'
    ),
    'v5p-5760': SystemCharacteristics(
      '12x12x20', 720, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-5760'
    ),
    'v5p-5888': SystemCharacteristics(
      '4x8x92', 736, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-5888'
    ),
    'v5p-6016': SystemCharacteristics(
      '4x4x188', 752, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6016'
    ),
    'v5p-6144': SystemCharacteristics(
      '12x16x16', 768, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6144'
    ),
    'v5p-6272': SystemCharacteristics(
      '4x28x28', 784, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6272'
    ),
    'v5p-6400': SystemCharacteristics(
      '8x20x20', 800, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6400'
    ),
    'v5p-6528': SystemCharacteristics(
      '4x12x68', 816, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6528'
    ),
    'v5p-6656': SystemCharacteristics(
      '8x8x52', 832, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6656'
    ),
    'v5p-6784': SystemCharacteristics(
      '4x4x212', 848, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6784'
    ),
    'v5p-6912': SystemCharacteristics(
      '12x12x24', 864, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6912'
    ),
    'v5p-7040': SystemCharacteristics(
      '4x20x44', 880, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7040'
    ),
    'v5p-7168': SystemCharacteristics(
      '8x16x28', 896, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7168'
    ),
    'v5p-7296': SystemCharacteristics(
      '4x12x76', 912, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7296'
    ),
    'v5p-7424': SystemCharacteristics(
      '4x8x116', 928, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7424'
    ),
    'v5p-7552': SystemCharacteristics(
      '4x4x236', 944, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7552'
    ),
    'v5p-7680': SystemCharacteristics(
      '12x16x20', 960, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7680'
    ),
    'v5p-7808': SystemCharacteristics(
      '4x4x244', 976, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7808'
    ),
    'v5p-7936': SystemCharacteristics(
      '4x8x124', 992, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7936'
    ),
    'v5p-8064': SystemCharacteristics(
      '12x12x28', 1008, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8064'
    ),
    'v5p-8192': SystemCharacteristics(
      '16x16x16', 1024, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8192'
    ),
    'v5p-8320': SystemCharacteristics(
      '4x20x52', 1040, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8320'
    ),
    'v5p-8448': SystemCharacteristics(
      '8x12x44', 1056, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8448'
    ),
    'v5p-8704': SystemCharacteristics(
      '8x8x68', 1088, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8704'
    ),
    'v5p-8832': SystemCharacteristics(
      '4x12x92', 1104, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8832'
    ),
    'v5p-8960': SystemCharacteristics(
      '8x20x28', 1120, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8960'
    ),
    'v5p-9216': SystemCharacteristics(
      '12x16x24', 1152, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-9216'
    ),
    'v5p-9472': SystemCharacteristics(
      '4x8x148', 1184, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-9472'
    ),
    'v5p-9600': SystemCharacteristics(
      '12x20x20', 1200, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-9600'
    ),
    'v5p-9728': SystemCharacteristics(
      '8x8x76', 1216, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-9728'
    ),
    'v5p-9856': SystemCharacteristics(
      '4x28x44', 1232, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-9856'
    ),
    'v5p-9984': SystemCharacteristics(
      '8x12x52', 1248, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-9984'
    ),
    'v5p-10240': SystemCharacteristics(
      '16x16x20', 1280, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-10240'
    ),
    'v5p-10368': SystemCharacteristics(
      '12x12x36', 1296, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-10368'
    ),
    'v5p-10496': SystemCharacteristics(
      '4x8x164', 1312, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-10496'
    ),
    'v5p-10752': SystemCharacteristics(
      '12x16x28', 1344, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-10752'
    ),
    'v5p-10880': SystemCharacteristics(
      '4x20x68', 1360, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-10880'
    ),
    'v5p-11008': SystemCharacteristics(
      '4x8x172', 1376, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-11008'
    ),
    'v5p-11136': SystemCharacteristics(
      '4x12x116', 1392, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-11136'
    ),
    'v5p-11264': SystemCharacteristics(
      '8x16x44', 1408, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-11264'
    ),
    'v5p-11520': SystemCharacteristics(
      '12x20x24', 1440, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-11520'
    ),
    'v5p-11648': SystemCharacteristics(
      '4x28x52', 1456, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-11648'
    ),
    'v5p-11776': SystemCharacteristics(
      '8x8x92', 1472, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-11776'
    ),
    'v5p-11904': SystemCharacteristics(
      '4x12x124', 1488, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-11904'
    ),
    'v5p-12032': SystemCharacteristics(
      '4x8x188', 1504, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-12032'
    ),
    'v5p-12160': SystemCharacteristics(
      '4x20x76', 1520, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-12160'
    ),
    'v5p-12288': SystemCharacteristics(
      '16x16x24', 1536, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-12288'
    ),
    'v5p-13824': SystemCharacteristics(
      '12x24x24', 1728, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-13824'
    ),
    'v5p-17920': SystemCharacteristics(
      '16x20x28', 2240, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-17920'
    ),
    # v5litepod
    'v5litepod-16': SystemCharacteristics(
        '4x4', 4, 'tpu-v5-lite-podslice', 'ct5lp-hightpu-4t', 4, AcceleratorType['TPU'], 'v5litepod-16'
    ),
    'v5litepod-32': SystemCharacteristics(
        '4x8', 8, 'tpu-v5-lite-podslice', 'ct5lp-hightpu-4t', 4, AcceleratorType['TPU'], 'v5litepod-32'
    ),
    'v5litepod-64': SystemCharacteristics(
        '8x8', 16, 'tpu-v5-lite-podslice', 'ct5lp-hightpu-4t', 4, AcceleratorType['TPU'], 'v5litepod-64'
    ),
    'v5litepod-128': SystemCharacteristics(
        '8x16', 32, 'tpu-v5-lite-podslice', 'ct5lp-hightpu-4t', 4, AcceleratorType['TPU'], 'v5litepod-128'
    ),
    'v5litepod-256': SystemCharacteristics(
        '16x16', 64, 'tpu-v5-lite-podslice', 'ct5lp-hightpu-4t', 4, AcceleratorType['TPU'], 'v5litepod-256'
    ),
    # v4
    'v4-8': SystemCharacteristics(
      '2x2x1', 1,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-8'
    ),
    'v4-16': SystemCharacteristics(
      '2x2x2', 2,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-16'
    ),
    'v4-32': SystemCharacteristics(
      '2x2x4', 4,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-32'
    ),
    'v4-64': SystemCharacteristics(
      '2x4x4', 8,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-64'
    ),
    'v4-128': SystemCharacteristics(
      '4x4x4', 16,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-128'
    ),
    'v4-256': SystemCharacteristics(
      '4x4x8', 32,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-256'
    ),
    'v4-512': SystemCharacteristics(
      '4x8x8', 64,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-512'
    ),
    'v4-1024': SystemCharacteristics(
      '8x8x8', 128,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-1024'
    ),
    'v4-1536': SystemCharacteristics(
      '8x8x12', 192,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-1536'
    ),
    'v4-2048': SystemCharacteristics(
      '8x8x16', 256,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-2048'
    ),
    'v4-4096': SystemCharacteristics(
      '8x16x16', 512,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-4096'
    ),

    # CPU system characteristics
    # n2-standard-32-$VMs
    'n2-standard-32-1': SystemCharacteristics(
      'N/A', 1,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-1'
    ),
    'n2-standard-32-2': SystemCharacteristics(
      'N/A', 2,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-2'
    ),
    'n2-standard-32-4': SystemCharacteristics(
      'N/A', 4,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-4'
    ),
    'n2-standard-32-8': SystemCharacteristics(
      'N/A', 8,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-8'
    ),
    'n2-standard-32-16': SystemCharacteristics(
      'N/A', 16,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-16'
    ),
    'n2-standard-32-32': SystemCharacteristics(
      'N/A', 32,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-32'
    ),
    'n2-standard-32-64': SystemCharacteristics(
      'N/A', 64,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-64'
    ),
    'n2-standard-32-128': SystemCharacteristics(
      'N/A', 128,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-128'
    ),
    'n2-standard-32-256': SystemCharacteristics(
      'N/A', 256,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-256'
    ),
    'n2-standard-32-512': SystemCharacteristics(
      'N/A', 512,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-512'
    ),
    'n2-standard-32-1024': SystemCharacteristics(
      'N/A', 1024,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-1024'
    ),
    'n2-standard-32-2048': SystemCharacteristics(
      'N/A', 2048,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-2048'
    ),
}
""" If you modify UserFacingNameToSystemCharacteristics you should also modify the corresponding
Map in MaxText/accelerator_to_spec_map.py """
# ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

def chunks(lst, n):
  """Return a list of n-sized chunks from lst.

  Args:
    lst: input list to get chunks from.
    n: size of each chunk.

  Returns:
    List of n-sized chunks for lst.
  """
  return [lst[i:i+n] for i in range(0, len(lst), n)]


def make_tmp_files(per_command_name):
  """Make temporary files for each command.

  Args:
    per_command_name: list of command names.

  Returns:
    A list of temporary files for each command.
  """
  return [
      tempfile.NamedTemporaryFile(delete=False, prefix=command + '-')
      for command in per_command_name
  ]


def run_commands(commands, jobname, per_command_name, batch=10, dry_run=False):
  """Run commands in groups of `batch`.

  Args:
    commands: list of command.
    jobname: the name of the job.
    per_command_name: list of command names.
    batch: number of commands to run in parallel.
    dry_run: enables dry_run if set to true.

  Returns:
    0 if successful and 1 otherwise.
  """
  temporary_files_batches = chunks(make_tmp_files(per_command_name), batch)
  commands_batched = chunks(commands, batch)
  per_command_name_batches = chunks(per_command_name, batch)

  xpk_print(
      f'Breaking up a total of {len(commands)} commands into'
      f' {len(commands_batched)} batches'
  )
  if dry_run:
    xpk_print('Pretending all the jobs succeeded')
    return 0

  max_return_code = 0
  for i, _ in enumerate(commands_batched):
    xpk_print(f'Dispatching batch {i}/{len(commands_batched)}')
    batch_max_return_code, _ = run_command_batch(
        commands_batched[i],
        jobname,
        per_command_name_batches[i],
        temporary_files_batches[i],
    )
    max_return_code = max(max_return_code, batch_max_return_code)
    if max_return_code > 0:
      return max_return_code
  return max_return_code


def run_command_batch(commands, jobname, per_command_name, output_logs):
  """Runs commands in parallel.

  Args:
    commands: list of n commands, each command is a a list of strings
    jobname: Useful debugging name for the group of commands
    per_command_name: specific name per task
    output_logs: list of n log paths, each command will output to each log.

  Returns:
    The max return code and a list of all the return codes.
  """

  children = []
  start_time = datetime.datetime.now()
  for i, command in enumerate(commands):
    children.append(
        # subprocess managed by list pylint: disable=consider-using-with
        subprocess.Popen(
            command, stdout=output_logs[i], stderr=output_logs[i], shell=True
        )
    )

  while True:
    returncodes = [child.poll() for child in children]
    max_returncode = max([0] + [r for r in returncodes if r is not None])
    completed = len([r for r in returncodes if r is not None])
    total = len(returncodes)
    seconds_elapsed = (datetime.datetime.now() - start_time).total_seconds()
    if completed < total:
      slow_worker_index = returncodes.index(None)
      slow_worker_text = per_command_name[slow_worker_index]
      slow_str = (
          f', task {slow_worker_text} still working, logfile'
          f' {output_logs[slow_worker_index].name}'
      )
    else:
      slow_str = ''
    xpk_print(
        f'[t={seconds_elapsed:.2f}, {jobname}] Completed'
        f' {completed}/{total}{slow_str}'
    )
    if max_returncode > 0:
      failing_index = [
          i for i, x in enumerate(returncodes) if x is not None and x > 0
      ][0]
      xpk_print(
          f'Terminating all {jobname} processes since at least one failed.'
      )
      xpk_print(
          f'Failure is {per_command_name[failing_index]}'
          f' and logfile {output_logs[failing_index].name}'
      )
      for child in children:
        child.terminate()
      break

    if completed == total:
      break

    time.sleep(1)
  return max_returncode, returncodes


def add_zone_and_project(args):
  """Obtains the zone and project names from gcloud configs if not defined.

  Args:
    args: user provided arguments for running the command.
  """
  if not args.project:
    args.project = get_project()
  if not args.zone:
    args.zone = get_zone()
  xpk_print(f'Working on {args.project=} and {args.zone}')


def add_env_config(args):
  """Adds environment configurations to the jobset config.

  Args:
    args: user provided arguments for running the command.
  """
  device_type = args.tpu_type if args.tpu_type else args.device_type
  env = {'JOBSET_NAME': args.workload}

  if args.env_file:
    print('Setting container environment from', args.env_file)
    pat = re.compile(r'(^[a-zA-Z_][a-zA-Z0-9_]*?)(?:=(.*))$', re.M)
    with open(file=args.env_file, mode='r', encoding='utf-8') as f:
      for match in pat.finditer(f.read()):
        variable = match.group(1)
        if len(match.groups()) > 1:
          env[variable] = match.group(2)
        else:
          assert variable in os.environ, (
              f'Variable {variable} is not set in the current '
              'environment, a value must be specified.'
          )
          env[variable] = os.environ[variable]

  if args.debug_dump_gcs:
    if args.use_pathways:
      xpk_print('HLO dumps need to be taken by Pathways workers.')
      xpk_exit(1)

    if 'XLA_FLAGS' in env:
      raise ValueError('Conflict: XLA_FLAGS defined in both --debug_dump_gcs '
                       'and environment file. Please choose one way to define '
                       'XLA_FLAGS.')
    env['XLA_FLAGS'] = '--xla_dump_to=/tmp/xla_dump/'


  if device_type == h100_device_type:
    # For H100, it has two more spaces ahead of name and value respectively
    env_format = '''
                  - name: {key}
                    value: "{value}"'''
  else:
    env_format = '''
                - name: {key}
                  value: "{value}"'''

  args.env = ''.join(env_format.format(key=k, value=v) for k, v in env.items())

def write_temporary_file(payload):
  """Writes `payload` to a temporary file.

  Args:
    payload: The string to be written to the file.

  Returns:
    A file object that was written to.
  """
  with tempfile.NamedTemporaryFile(delete=False) as tmp:
    with open(file=tmp.name, mode='w', encoding='utf=8') as f:
      f.write(payload)
      f.flush()
    return tmp


def append_temporary_file(payload, file):
  """Appends `payload` to an already created file.

  Use `write_temporary_file` to create a file.

  Args:
    payload: The string to be written to the file.
    file: The file to append to.

  Returns:
    A file object that was written to.
  """
  with open(file=file.name, mode='a', encoding='utf=8') as f:
    f.write(payload)
    f.flush()
  return file


def run_command_for_value(
    command, task, global_args, dry_run_return_val='0'
) -> tuple[int, str]:
  """Runs the command and returns the error code and stdout.

  Prints errors and associated user-facing information

  Args:
    command: user provided command to run.
    task: user provided task name for running the command.
    global_args: user provided arguments for running the command.
    dry_run_return_val: return value of this command for dry run.

  Returns:
    tuple[int, str]
    int: return_code, default is 0
    str: return_val, default is '0'
  """
  if global_args.dry_run:
    xpk_print(
        f'Task: `{task}` is implemented by the following command'
        ' not running since it is a dry run.'
        f' \n{command}'
    )
    return 0, dry_run_return_val
  else:
    xpk_print(
        f'Task: `{task}` is implemented by `{command}`, hiding output unless'
        ' there is an error.'
    )
    try:
      output = subprocess.check_output(
          command,
          shell=True,
          stderr=subprocess.STDOUT,
      )
    except subprocess.CalledProcessError as e:
      xpk_print(f'Task {task} failed with {e.returncode}')
      xpk_print('*' * 80)
      xpk_print(e.output)
      xpk_print('*' * 80)
      return e.returncode, str(e.output, 'UTF-8')
    return 0, str(output, 'UTF-8')


def run_command_with_updates_retry(command, task, args, verbose=True, num_retry_attempts=5, wait_seconds=10) -> int:
  """Generic run commands function with updates and retry logic.

  Args:
    command: command to execute
    task: user-facing name of the task
    args: user provided arguments for running the command.
    verbose: shows stdout and stderr if set to true. Set to True by default.
    num_retry_attempts: number of attempts to retry the command. This has a default value in the function arguments.
    wait_seconds: Seconds to wait between attempts. Has a default value in the function arguments.

  Returns:
    0 if successful and 1 otherwise.
  """

  i = 0
  return_code = -1
  while (return_code != 0 and i < num_retry_attempts):
    # Do not sleep before first try.
    if i != 0:
      xpk_print(f'Wait {wait_seconds} seconds before retrying.')
      time.sleep(wait_seconds)
    i += 1
    xpk_print(f'Try {i}: {task}')
    return_code = run_command_with_updates(command, task, args, verbose=verbose)
  return return_code


def run_command_with_updates(command, task, global_args, verbose=True) -> int:
  """Generic run commands function with updates.

  Args:
    command: command to execute
    task: user-facing name of the task
    global_args: user provided arguments for running the command.
    verbose: shows stdout and stderr if set to true. Set to True by default.

  Returns:
    0 if successful and 1 otherwise.
  """
  if global_args.dry_run:
    xpk_print(
        f'Task: `{task}` is implemented by the following command'
        ' not running since it is a dry run.'
        f' \n{command}'
    )
    return 0
  if verbose:
    xpk_print(
        f'Task: `{task}` is implemented by `{command}`, streaming output live.'
    )
    with subprocess.Popen(
        command,
        stdout=sys.stdout,
        stderr=sys.stderr,
        shell=True,
    ) as child:
      i = 0
      while True:
        return_code = child.poll()
        if return_code is None:
          xpk_print(f'Waiting for `{task}`, for {i} seconds')
          time.sleep(1)
          i += 1
        else:
          xpk_print(f'Task: `{task}` terminated with code `{return_code}`')
          return return_code
  else:
    xpk_print(
        f'Task: `{task}` is implemented by `{command}`, hiding output unless'
        ' there is an error.'
    )
    try:
      subprocess.check_output(command, shell=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
      xpk_print(
          f'Task: `{task}` terminated with ERROR `{e.returncode}`, printing'
          ' logs'
      )
      xpk_print('*' * 80)
      xpk_print(e.output)
      xpk_print('*' * 80)
      return e.returncode
    xpk_print(f'Task: `{task}` succeeded.')
    return 0


def xpk_print(*args, **kwargs):
  """Helper function to print a prefix before function provided args.

  Args:
    *args: user provided print args.
    **kwargs: user provided print args.
  """
  sys.stdout.write('[XPK] ')
  print(*args, **kwargs)
  sys.stdout.flush()


def xpk_exit(error_code):
  """Helper function to exit xpk with an associated error code.

  Args:
    error_code: If the code provided is zero, then no issues occurred.
  """
  if error_code == 0:
    xpk_print('Exiting XPK cleanly')
    sys.exit(0)
  else:
    xpk_print(f'XPK failed, error code {error_code}')
    sys.exit(error_code)


def get_project():
  """Get GCE project from `gcloud config get project`.

  Returns:
     The project name.
  """
  completed_command = subprocess.run(
      ['gcloud', 'config', 'get', 'project'], check=True, capture_output=True
  )
  project_outputs = completed_command.stdout.decode().strip().split('\n')
  if len(project_outputs) < 1 or project_outputs[-1] == '':
    sys.exit(
        'You must specify the project in the project flag or set it with'
        " 'gcloud config set project <project>'"
    )
  return project_outputs[
      -1
  ]  # The project name lives on the last line of the output


def get_zone():
  """Get GCE zone from `gcloud config get compute/zone`.

  Returns:
     The zone name.
  """
  completed_command = subprocess.run(
      ['gcloud', 'config', 'get', 'compute/zone'],
      check=True,
      capture_output=True,
  )
  zone_outputs = completed_command.stdout.decode().strip().split('\n')
  if len(zone_outputs) < 1 or zone_outputs[-1] == '':
    sys.exit(
        "You must specify the zone in the zone flag or set it with 'gcloud"
        " config set compute/zone <zone>'"
    )
  return zone_outputs[-1]  # The zone name lives on the last line of the output


def zone_to_region(zone) -> str:
  """Helper function converts zone name to region name.

  Args:
    zone: zone name.

  Returns:
     The region name.
  """
  zone_terms = zone.split('-')
  return zone_terms[0] + '-' + zone_terms[1]


def get_service_account_name(args) -> str:
  """Get the name for the service account.
  Args:
    args: user provided arguments.

  Returns:
    the name of the service account.
  """
  return f'{args.project}-{_XPK_SERVICE_ACCOUNT}@{args.project}.iam.gserviceaccount.com'

def check_if_service_account_exists(args) -> bool:
  """Check if a service account with the given name exists in the project.

  Args:
    args: user provided arguments for running the command.

  Returns:
    True if service account exist, False otherwise.
  """
  service_account_name = get_service_account_name(args)
  command = (
    f'gcloud iam service-accounts describe {service_account_name}'
  )
  return_code = run_command_with_updates(command, 'Service Account Describe', args, verbose=False)
  if return_code != 0:
    xpk_print(f'Service Account Describe did not find the service account {service_account_name}.')
    return False
  return True

def create_service_account(args) -> int:
  """Creates a service account in the project.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
    f'gcloud iam service-accounts create {args.project}-{_XPK_SERVICE_ACCOUNT} \
    --description="Service Account for XPK" \
    --display-name="{args.project}-{_XPK_SERVICE_ACCOUNT}"'
  )
  return_code = run_command_with_updates(command, 'Service Account Create', args)
  if return_code != 0:
    xpk_print(f'Service Account Create request returned ERROR {return_code}')
    xpk_print('Make sure you have Service Account Admin Role attached to your user.')
    return 1
  return 0

def get_existing_roles_in_service_account(args) -> set:
  """
  Args:
    args: user provided arguments for running the command.

  Returns:
    set of IAM roles already attached to the service account.
  """
  roles = []
  service_account_name = get_service_account_name(args)
  command = (
    f'gcloud projects get-iam-policy {args.project}'
    f' --filter="bindings.members:{service_account_name}"'
    ' --flatten="bindings[].members" --format="table(bindings.role)"'
  )
  return_code, return_value = run_command_for_value(command, 'Get IAM Roles For Service Account', args)
  if return_code != 0:
    xpk_print(f'Get IAM Roles For Service Account request returned ERROR {return_code}')
  else:
    return_value = return_value.strip()
    roles = set(return_value.split('\n'))
    """Format of return_value is:
          ROLE
          roles/storage.admin
          roles/logging.admin
      removing `ROLE` from the list
    """
    if 'ROLE' in roles:
      roles.remove('ROLE')
  return roles


def add_roles_to_service_account(args) -> int:
  """Add IAM roles to service account.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  service_account_name = get_service_account_name(args)
  existing_roles = get_existing_roles_in_service_account(args)
  xpk_print(f'IAM roles already attached to service account: {existing_roles}')

  for name, role in IAMRoles.items():
    if role in existing_roles:
      continue

    xpk_print(f'Adding {name} role to service account: {service_account_name}.')
    command = (
      f'gcloud projects add-iam-policy-binding {args.project} \
      --member="serviceAccount:{service_account_name}" \
      --role="{role}" \
      --condition=None'
    )
    return_code = run_command_with_updates(command, 'Add IAM Role to Service Account', args, verbose=False)
    if return_code != 0:
      xpk_print(f'Add IAM Role to Service Account request returned ERROR {return_code}')
      xpk_print('Make sure you have Project IAM Admin Role attached to your user.')
      return 1
  return 0


def run_gke_cluster_create_command(args) -> int:
  """Run the Create GKE Cluster request.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  # cluster_cpu_machine_type is soon to be deprecated!
  machine_type = args.default_pool_cpu_machine_type
  if args.cluster_cpu_machine_type != '':
    xpk_print('Warning: Note that cluster-cpu-machine-type is soon to be',
              ' deprecated. Please use --default-pool-cpu-machine-type instead,'
              ' to denote the machine type of the default cpu node pool. Set'
              ' the machine type of other cpu nodepools using `--device-type`.')
    machine_type = args.cluster_cpu_machine_type

  command = (
      'gcloud beta container clusters create'
      f' {args.cluster} --project={args.project}'
      f' --region={zone_to_region(args.zone)}'
      f' --node-locations={args.zone}'
      f' --cluster-version={args.gke_version}'
      f' --machine-type={machine_type}'
  )

  if _SERVICE_ACCOUNT_FEATURE_FLAG:
    service_account_name = get_service_account_name(args)
    service_account_exists = check_if_service_account_exists(args)
    if service_account_exists:
      command += f' --service-account={service_account_name}'
    else:
      xpk_print(f'Service Account: {service_account_name} does not exist in the project.'
              ' Will attach the default service account to the cluster.')

  device_type = args.tpu_type if args.tpu_type else args.device_type
  if device_type == h100_device_type:
    command += (' --enable-dataplane-v2 --enable-ip-alias'
        ' --enable-multi-networking --no-enable-autoupgrade')
  else:
    # Create the regional cluster with `num-nodes` CPU nodes in the same zone as
    # TPUs. This has been tested with clusters of 300 VMs. Larger clusters will
    # benefit from a larger initial `--num-nodes`. After the cluster is created,
    # the auto-scaler can reduce/increase the nodes based on the load.

    command += (' --release-channel rapid --enable-autoscaling --location-policy=BALANCED'
        ' --scopes=storage-full,gke-default'
        ' --total-min-nodes 1 --total-max-nodes 1000 --num-nodes 6'
        f' {args.custom_cluster_arguments}'
    )

    if args.enable_pathways:
      command += (' --enable-ip-alias ')
      command += (f' --create-subnetwork name={args.cluster}-subnetwork')

  return_code = run_command_with_updates(command, 'GKE Cluster Create', args)
  if return_code != 0:
    xpk_print(f'GKE Cluster Create request returned ERROR {return_code}')
    return 1

  return 0


def set_up_cluster_network_for_a3(args) -> int:
  """Set up GKE Cluster networks, subnets and firewall rules for A3.
  Note: there are 4 NICs for GPU-GPU bw and 1 NIC for host in a A3 node

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  for i in range (1, 5):
    return_code = create_cluster_network(args, i)
    if return_code != 0:
      return 1
    return_code = create_cluster_subnet(args, i)
    if return_code != 0:
      return 1
    return_code = create_cluster_firewall_rule(args, i)
    if return_code != 0:
      return 1
  return 0


def create_cluster_network(args, index) -> int:
  """Create one GKE Cluster network.

  Args:
    args: user provided arguments for running the command.
    index: index number for the network to be created.

  Returns:
    0 if successful and 1 otherwise.
  """
  existing_network_names, return_code = get_all_networks_programmatic(args)
  if return_code > 0:
    xpk_print('Listing all networks failed!')
    return return_code

  network_name = f'{args.cluster}-net-{index}'
  if network_name not in existing_network_names:
    command = (
      f'gcloud compute --project={args.project}'
      f' networks create {network_name}'
      ' --subnet-mode=custom --mtu=8244'
    )
    return_code = run_command_with_updates(
        command, 'Create Cluster Network', args, verbose=False
    )

    if return_code != 0:
      xpk_print(f'Create Cluster Network request returned ERROR {return_code}')
      return 1
  else:
    xpk_print(f'Reusing existing network {network_name}')

  return 0


def create_cluster_subnet(args, index) -> int:
  """Create one GKE Cluster subnet.

  Args:
    args: user provided arguments for running the command.
    index: index number for the subnet to be created.

  Returns:
    0 if successful and 1 otherwise.
  """
  existing_subnet_names, return_code = get_all_subnets_programmatic(args)
  if return_code > 0:
    xpk_print('Listing all subnets failed!')
    return return_code
  subnet_name = f'{args.cluster}-sub-{index}'
  if subnet_name not in existing_subnet_names:
    command = (
      f'gcloud compute --project={args.project}'
      f' networks subnets create {subnet_name}'
      f' --network={args.cluster}-net-{index}'
      f' --region={zone_to_region(args.zone)} --range=192.168.{index}.0/24'
    )
    return_code = run_command_with_updates(
        command, 'Create Cluster Subnet', args, verbose=False
    )

    if return_code != 0:
      xpk_print(f'Create Cluster Subnet request returned ERROR {return_code}')
      return 1
    else:
      xpk_print(f'Reusing existing subnet {subnet_name}')

  return 0


def create_cluster_firewall_rule(args, index) -> int:
  """Create one GKE Cluster firewall rule.

  Args:
    args: user provided arguments for running the command.
    index: index number for the firewall rule to be created.

  Returns:
    0 if successful and 1 otherwise.
  """
  existing_firewall_rules_names, return_code = get_all_firewall_rules_programmatic(args)
  if return_code > 0:
    xpk_print('Listing all firewall rules failed!')
    return return_code
  firewall_rule_name = f'{args.cluster}-internal-{index}'
  if firewall_rule_name not in existing_firewall_rules_names:
    command = (
      f'gcloud compute --project={args.project}'
      f' firewall-rules create {firewall_rule_name}'
      f' --network={args.cluster}-net-{index}'
      ' --action=ALLOW --rules=tcp:0-65535,udp:0-65535,icmp --source-ranges=192.168.0.0/16'
    )
    return_code = run_command_with_updates(
        command, 'Create Cluster Firewall Rule', args, verbose=False
    )

    if return_code != 0:
      xpk_print(f'Create Cluster Firewall Rule request returned ERROR {return_code}')
      return 1
  else:
    xpk_print(f'Reusing existing firewall rule {firewall_rule_name}')
  return 0


def create_cluster_network_config(args) -> int:
  """Run the Create GKE Cluster Network Config request.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  yml_string = cluster_network_yaml.format(cluster_name=args.cluster)
  tmp = write_temporary_file(yml_string)
  command = f'kubectl apply -f {str(tmp.file.name)}'

  return_code = run_command_with_updates(command, 'GKE Cluster Create Network Config', args)
  if return_code != 0:
    xpk_print(f'GKE Cluster Create ConfigMap request returned ERROR {return_code}')
    return 1

  return 0

def create_cluster_configmaps(args, system):
  """Run the Create GKE Cluster ConfigMap request.

  Args:
    args: user provided arguments for running the command.
    system: system characteristics.

  Returns:
    0 if successful and 1 otherwise.
  """
  configmap_yml = {}

  # ConfigMap to store resources available in the cluster
  device_type = args.tpu_type if args.tpu_type else args.device_type
  if device_type == h100_device_type:
    resources_data = f'{device_type}: "{int(args.num_nodes)}"'
  else:
    resources_data = f'{device_type}: "{int(args.num_slices) * system.vms_per_slice}"'
  resources_configmap_name = f'{args.cluster}-{_CLUSTER_RESOURCES_CONFIGMAP}'
  resources_yml = cluster_configmap_yaml.format(args=args,
                                                name=resources_configmap_name,
                                                data=resources_data)
  configmap_yml[resources_configmap_name] = resources_yml

  # ConfigMap to store cluster metadata like xpk_version
  metadata = f'xpk_version: {xpk_current_version}'
  metadata_configmap_name = f'{args.cluster}-{_CLUSTER_METADATA_CONFIGMAP}'
  metadata_yml = cluster_configmap_yaml.format(args=args,
                                               name=metadata_configmap_name,
                                               data=metadata)
  configmap_yml[metadata_configmap_name] = metadata_yml

  commands = []
  task_names = []
  for configmap_name, yml_string in configmap_yml.items():
    tmp = write_temporary_file(yml_string)
    command = f'kubectl apply -f {str(tmp.file.name)}'
    commands.append(command)
    task_name = f'ConfigMapCreate-{configmap_name}'
    task_names.append(task_name)

  return_code = run_commands(commands, 'GKE Cluster Create ConfigMap(s)', task_names)
  if return_code != 0:
    xpk_print(f'GKE Cluster Create ConfigMap request returned ERROR {return_code}')
    return 1

  return 0


def get_cluster_configmap(args, configmap_name):
  """Run the Get GKE Cluster ConfigMap request.

  Args:
    args: user provided arguments for running the command.
    configmap_name: name of the configmap.

  Returns:
    key:value pairs stored in cluster ConfigMap.
  """
  command = (
    f'kubectl get configmap {configmap_name} -o=custom-columns="ConfigData:data" --no-headers=true'
  )

  return_code, return_value = run_command_for_value(command, 'GKE Cluster Get ConfigMap', args)
  if return_code != 0:
    xpk_print(f'GKE Cluster Get ConfigMap request returned ERROR {return_code}')
    return None

  config_map = {}
  return_value = return_value.strip()

  if return_value:
    # Format of ConfigMap: map[key1:value1 key2:value2]
    return_value = return_value[return_value.index("map"):]
    configs = return_value[4:-1].split(" ")

    for config in configs:
      key, value = config.strip().split(":")
      config_map[key] = int(value)
  return config_map


def get_all_clusters_programmatic(args) -> tuple[list[str], int]:
  """Gets all the clusters associated with the project / region.

  Args:
    args: user provided arguments for running the command.

  Returns:
    List of cluster names and 0 if successful and 1 otherwise.
  """
  command = (
      'gcloud container clusters list'
      f' --project={args.project} --region={zone_to_region(args.zone)}'
  )
  return_code, raw_cluster_output = run_command_for_value(
      command, 'Find if Cluster Exists', args
  )
  if return_code != 0:
    xpk_print(f'Find if Cluster Exists returned ERROR {return_code}')
    return [], return_code
  cluster_names = [x.split(' ')[0] for x in raw_cluster_output.splitlines()]
  return cluster_names, 0


def create_cluster_if_necessary(args) -> int:
  all_clusters, return_code = get_all_clusters_programmatic(args)
  if return_code > 0:
    xpk_print('Listing all clusters failed!')
    return 1
  if args.cluster in all_clusters:
    xpk_print('Skipping cluster creation since it already exists')
    return 0
  else:
    return run_gke_cluster_create_command(args)


def get_all_nodepools_programmatic(args) -> tuple[list[str], int]:
  """Gets all the nodepools associated with the cluster / project / region.

  Args:
    args: user provided arguments for running the command.

  Returns:
    List of nodepools and 0 if successful and 1 otherwise.
  """
  command = (
      'gcloud beta container node-pools list'
      ' --cluster'
      f' {args.cluster} --project={args.project} --region={zone_to_region(args.zone)}'
  )
  return_code, raw_nodepool_output = (
      run_command_for_value(command, 'Get All Node Pools', args)
  )
  if return_code != 0:
    xpk_print(f'Get All Node Pools returned ERROR {return_code}')
    return [], 1

  all_nodepools = [x.split(' ')[0] for x in raw_nodepool_output.splitlines()]
  return all_nodepools, 0

def get_all_networks_programmatic(args) -> tuple[list[str], int]:
  """Gets all the networks associated with project .

  Args:
    args: user provided arguments for running the command.

  Returns:
    List of networks and 0 if successful and 1 otherwise.
  """
  command = (
      'gcloud compute networks list'
  )
  return_code, raw_network_output = (
      run_command_for_value(command, 'Get All Networks', args)
  )
  if return_code != 0:
    xpk_print(f'Get All Networks returned ERROR {return_code}')
    return [], 1

  all_networks = [x.split(' ')[0] for x in raw_network_output.splitlines()]
  return all_networks, 0

def get_all_subnets_programmatic(args) -> tuple[list[str], int]:
  """Gets all the subnets associated with the project.

  Args:
    args: user provided arguments for running the command.

  Returns:
    List of subnets and 0 if successful and 1 otherwise.
  """
  command = (
      'gcloud compute networks subnets list'
  )
  return_code, raw_subnets_output = (
      run_command_for_value(command, 'Get All Subnets', args)
  )
  if return_code != 0:
    xpk_print(f'Get All Subnets returned ERROR {return_code}')
    return [], 1

  all_networks = [x.split(' ')[0] for x in raw_subnets_output.splitlines()]
  return all_networks, 0


def get_all_firewall_rules_programmatic(args) -> tuple[list[str], int]:
  """Gets all the firewall rules associated with the project.

  Args:
    args: user provided arguments for running the command.

  Returns:
    List of firewall rules and 0 if successful and 1 otherwise.
  """
  command = (
      'gcloud compute firewall-rules list'
  )
  return_code, raw_subnets_output = (
      run_command_for_value(command, 'Get All Firewall Rules', args)
  )
  if return_code != 0:
    xpk_print(f'Get All Firewall Rules returned ERROR {return_code}')
    return [], 1

  all_networks = [x.split(' ')[0] for x in raw_subnets_output.splitlines()]
  return all_networks, 0


def print_reservations(args) -> int:
  """Print the reservations in the project.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
        f'gcloud beta compute reservations list --project={args.project}'
    )
  return_code = (
      run_command_with_updates(
          command, 'Get all reservations in the project', args)
  )
  if return_code != 0:
    xpk_print(f'Get all reservations returned ERROR {return_code}')
    return 1
  return 0


def verify_reservation_exists(args) -> int:
  """Verify the reservation exists.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
        f'gcloud beta compute reservations describe {args.reservation}'
        f' --project={args.project} --zone={args.zone}'
    )
  return_code = (
      run_command_with_updates(
          command, 'Describe reservation', args)
  )
  if return_code != 0:
    xpk_print(f'Describe reservation returned ERROR {return_code}')
    xpk_print('Please confirm that your reservation name is correct.')
    return 1
  return 0


def get_capacity_arguments(args) -> tuple[str, int]:
  """Determine the TPU Nodepool creation capacity arguments needed.

  Args:
    args: user provided arguments for running the command.

  Returns:
    Tuple with string with the capacity argument to use and
    int of 0 if successful and 1 otherwise.
  """
  capacity_args = ""
  num_types = 0
  return_code = 0

  # Determine the capacity argument.
  if args.on_demand:
    capacity_args = ""
    num_types+=1
  if args.reservation:
    return_code = verify_reservation_exists(args)
    if return_code > 0:
      return capacity_args, return_code
    capacity_args = (
      f'--reservation-affinity=specific --reservation={args.reservation}'
    )
    num_types+=1
  if args.spot:
    capacity_args = "--spot"
    num_types+=1

  # Check that the number of arguments provided is valid.
  if num_types == 0:
    return_code = print_reservations(args)
    xpk_print(
      'ERROR: User needs to provide the capacity type. Please specify one of'
      ' the following `--reservation=$RESERVATION_NAME`, `--on-demand`'
      ' or `--spot`. See the above list of reservations to choose from.'
    )
    if return_code > 0:
      xpk_print('Listing all reservations failed!')
    return_code = 1
  elif num_types != 1:
    xpk_print(
      'ERROR: User specified more than one of the following arguments. Please'
      ' specify only one of `--reservation=$RESERVATION_NAME`, `--on-demand`'
      ' or `--spot`.'
    )
    return_code = 1

  return capacity_args, return_code


def get_user_input(input_msg):
  """Function to get the user input for a prompt.

  Args:
    input_msg: message to be displayed by the prompt.
  Returns:
    True if user enter y or yes at the prompt, False otherwise.
  """
  user_input = input(input_msg)
  return user_input in ('y', 'yes')


def run_gke_node_pool_create_command(args, system) -> int:
  """Run the Create GKE Node Pool request.

  Args:
    args: user provided arguments for running the command.
    system: System characteristics based on device type/topology.

  Returns:
    0 if successful and 1 otherwise.
  """
  device_type = args.tpu_type if args.tpu_type else args.device_type
  xpk_print(
      f'Creating {args.num_slices} node pool or pools of {device_type}\n'
      f'We assume that the underlying system is: {system}'
  )
  existing_node_pool_names, return_code = get_all_nodepools_programmatic(args)
  if return_code > 0:
    xpk_print('Listing all node pools failed!')
    return return_code

  capacity_args, return_code = get_capacity_arguments(args)
  if return_code > 0:
    xpk_print('Parsing capacity arguments failed!')
    return return_code

  commands = []
  task_names = []

  if system.accelerator_type == AcceleratorType['GPU']:
    xpk_print(
      f'Creating 1 node pool with {args.num_nodes} nodes of {system.device_type}\n'
      f'Underlyingly, we assume that means: {system}'
    )
    desired_node_pool_names = [f'{args.cluster}-np-0']
  else:
    xpk_print(
      f'Creating {args.num_slices} node pool or pools of {system.device_type}\n'
      f'Underlyingly, we assume that means: {system}'
    )
    desired_node_pool_names = [
      f'{args.cluster}-np-{slice_num}' for slice_num in range(args.num_slices)
    ]

  for node_pool_name in desired_node_pool_names:
    if node_pool_name in existing_node_pool_names:
      continue
    command = (
        'gcloud beta container node-pools create'
        f' {node_pool_name}'
        f' --region={zone_to_region(args.zone)}'
        f' --cluster={args.cluster}'
        f' --project={args.project} --node-locations={args.zone}'
        f' --machine-type={system.gce_machine_type}'
        f' --host-maintenance-interval={args.host_maintenance_interval}'
        f' {capacity_args}'
        ' --enable-gvnic'
    )
    if system.accelerator_type == AcceleratorType['TPU']:
      command += (f' --node-version={args.gke_version}')
      command += (f' --num-nodes={system.vms_per_slice}')
      command += (' --placement-type=COMPACT  --max-pods-per-node 15')
      command += (' --scopes=storage-full,gke-default')
      command += (f' --tpu-topology={system.topology}')
      command += (f' {args.custom_tpu_nodepool_arguments}')
    elif system.accelerator_type == AcceleratorType['GPU']:
      command += (f' --num-nodes={args.num_nodes}')
      command += (f' --accelerator type={system.gke_accelerator},count={str(system.chips_per_vm)}'
        f' --additional-node-network network={args.cluster}-net-1,subnetwork={args.cluster}-sub-1'
        f' --additional-node-network network={args.cluster}-net-2,subnetwork={args.cluster}-sub-2'
        f' --additional-node-network network={args.cluster}-net-3,subnetwork={args.cluster}-sub-3'
        f' --additional-node-network network={args.cluster}-net-4,subnetwork={args.cluster}-sub-4'
        ' --no-enable-autoupgrade  --scopes="https://www.googleapis.com/auth/cloud-platform"'
        )

    if _SERVICE_ACCOUNT_FEATURE_FLAG:
      service_account_name = get_service_account_name(args)
      service_account_exists = check_if_service_account_exists(args)
      if service_account_exists:
        command += f' --service-account={service_account_name}'
      else:
        xpk_print(f'Service Account: {service_account_name} does not exist in the project.'
                ' Will attach the default service account to the node pools.')
    task = f'NodepoolCreate-{node_pool_name}'
    commands.append(command)
    task_names.append(task)

  desired_pw_cpu_node_pools = ['cpu-user-np', 'cpu-rm-np', 'cpu-proxy-np']
  if args.enable_pathways:
    # Pathways needs CPU nodepools in addition to TPU nodepools
    for node_pool_name in desired_pw_cpu_node_pools:
      if node_pool_name in existing_node_pool_names:
        continue
      command = (
          'gcloud beta container node-pools create'
          f' {node_pool_name} --node-version={args.gke_version}'
          f' --cluster={args.cluster}'
          f' --project={args.project} --node-locations={args.zone}'
          f' --region={zone_to_region(args.zone)}'
          f' --num-nodes=1'
          f' --machine-type={args.pathways_gce_machine_type}'
          ' --scopes=storage-full,gke-default'
          ' --enable-autoscaling --min-nodes=1 --max-nodes=20'
      )
      task = f'NodepoolCreate-{node_pool_name}'
      commands.append(command)
      task_names.append(task)

  # Deletion logic would leave behind any Pathways CPU nodepools.
  node_pools_to_delete = []
  for existing_node_pool_name in existing_node_pool_names:
    if (
        existing_node_pool_name.find(f'{args.cluster}-np-') == 0
        and existing_node_pool_name not in desired_node_pool_names
    ):
      node_pools_to_delete.append(existing_node_pool_name)

  will_delete = True
  if node_pools_to_delete and not args.force:
    will_delete = get_user_input(
      f'Planning to delete {len(node_pools_to_delete)} node pools including '
      f'{node_pools_to_delete}. \nDo you wish to delete: y (yes) / n (no):\n')

  if not will_delete:
    xpk_print('Skipping delete commands. Continuing to next step.')
  else:
    for existing_node_pool_name in node_pools_to_delete:
      if (
          existing_node_pool_name.find(f'{args.cluster}-np-') == 0
          and existing_node_pool_name not in desired_node_pool_names
      ):
        command = (
            'gcloud beta container node-pools delete'
            f' {existing_node_pool_name} --cluster={args.cluster}'
            f' --zone={zone_to_region(args.zone)}'
            f' --project={args.project} --quiet'
        )
        task = f'Nodepool-Delete-{existing_node_pool_name}'
        commands.append(command)
        task_names.append(task)

  for i, command in enumerate(commands):
    xpk_print(f'To complete {task_names[i]} we are executing {command}')
  max_return_code = run_commands(
      commands, 'Create and Delete Nodepools', task_names, dry_run=args.dry_run
  )
  if max_return_code != 0:
    xpk_print(f'Create and Delete Nodepools returned ERROR {max_return_code}')
    return 1

  xpk_print('Create or delete node pool request complete.')
  return 0


def run_gke_cluster_delete_command(args) -> int:
  """Run the Delete GKE Cluster request.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
      'gcloud beta container clusters delete'
      f' {args.cluster} --project={args.project} --region={zone_to_region(args.zone)} --quiet'
  )

  return_code = run_command_with_updates(command, 'Cluster Delete', args)

  if return_code != 0:
    xpk_print(f'Cluster delete request returned ERROR {return_code}')
    return 1

  return 0


def run_gke_clusters_list_command(args) -> int:
  """List GKE Clusters within the project and location.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
      'gcloud container clusters list'
      f' --project={args.project} --region={zone_to_region(args.zone)}'
  )
  return_code = run_command_with_updates(command, 'Cluster List', args)
  if return_code != 0:
    xpk_print(f'Cluster list request returned ERROR {return_code}')
    return 1

  return 0


def set_cluster_command(args) -> int:
  """Run cluster configuration command to set the kubectl config.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
      'gcloud container clusters get-credentials'
      f' {args.cluster} --region={zone_to_region(args.zone)} --project={args.project} &&'
      ' kubectl config view && kubectl config set-context --current --namespace=default'
  )
  task = f'get-credentials to cluster {args.cluster}'
  return_code = run_command_with_updates_retry(command, task, args, verbose=False)
  if return_code != 0:
    xpk_print(f'{task} returned ERROR {return_code}')
  return return_code


def install_kueue_on_cluster(args) -> int:
  """Install Kueue on the cluster.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
      'kubectl apply --server-side --force-conflicts -f'
      ' https://github.com/kubernetes-sigs/kueue/releases/download/v0.6.1/manifests.yaml'
  )
  task = 'Set Kueue On Cluster'
  return_code = run_command_with_updates_retry(command, task, args)
  if return_code != 0:
    xpk_print(f'{task} returned ERROR {return_code}')
  return return_code


def enable_kueue_crds(args, system) -> int:
  """Enable Kueue crds.

  Args:
    args: user provided arguments for running the command.
    system: system level arguments.

  Returns:
    0 if successful and 1 otherwise.
  """

  device_type = args.tpu_type if args.tpu_type else args.device_type
  cluster_hardware_name = f'{args.num_slices}x{device_type}'
  resource_type=AcceleratorTypeToAcceleratorCharacteristics[system.accelerator_type].resource_type
  if system.accelerator_type == AcceleratorType['GPU']:
    total_chips = args.num_nodes * system.chips_per_vm
  else:
    total_chips = args.num_slices * system.vms_per_slice * system.chips_per_vm
  covered_resources_config = get_kueue_covered_resources_config(
    args=args,
    cluster_hardware_name=cluster_hardware_name,
    resource_type=resource_type,
    total_chips=total_chips)
  yml_string = cluster_set_crd_yaml.format(
      system=system,
      cluster_hardware_name=cluster_hardware_name,
      accelerator_label=create_accelerator_label(system.accelerator_type, system),
      machine_label=create_machine_label(system.accelerator_type, system),
      covered_resources_config=covered_resources_config,
      resource_type=AcceleratorTypeToAcceleratorCharacteristics[system.accelerator_type].resource_type,
      pw_resource_flavors=add_pw_resource_flavors(args),
      pw_resources_kueue=add_pw_resources_to_kueue(args),
      cluster_queue_name=_CLUSTER_QUEUE_NAME,
      local_queue_name=_LOCAL_QUEUE_NAME,
  )

  tmp = write_temporary_file(yml_string)
  command = f'kubectl apply -f {str(tmp.file.name)}'
  # For kueue setup, we see a timeout error due to the webhook not
  # being ready. Let's retry and wait a few seconds.
  task = 'Applying Kueue CRDs'
  return_code = run_command_with_updates_retry(command, task, args)
  if return_code != 0:
    xpk_print(f'{task} returned ERROR {return_code}')
  return return_code

# TODO(roshanin): Organize Pathways helpers in another file.
def add_pw_resource_flavors(args):
  """Add resource flavors required for Pathways enabled clusters.
  """
  resource_flavor_yaml="""apiVersion: kueue.x-k8s.io/v1beta1
kind: ResourceFlavor
metadata:
  name: cpu-rm
spec:
  nodeLabels:
    cloud.google.com/gke-nodepool: cpu-rm-np
---
apiVersion: kueue.x-k8s.io/v1beta1
kind: ResourceFlavor
metadata:
  name: cpu-proxy
spec:
  nodeLabels:
    cloud.google.com/gke-nodepool: cpu-proxy-np
---
apiVersion: kueue.x-k8s.io/v1beta1
kind: ResourceFlavor
metadata:
  name: cpu-user
spec:
  nodeLabels:
    cloud.google.com/gke-nodepool: cpu-user-np
---"""
  if args.enable_pathways:
    return resource_flavor_yaml
  return ""


def add_pw_resources_to_kueue(args):
  """Add resource flavors required for Pathways, to the cluster queue.
  """
  resources_yaml="""- coveredResources: ["cpu", "memory"]
    flavors:
    - name: cpu-rm
      resources:
      - name: "cpu"
        nominalQuota: 80
      - name: "memory"
        nominalQuota: 160G
    - name: cpu-proxy
      resources:
      - name: "cpu"
        nominalQuota: 480
      - name: "memory"
        nominalQuota: 2000G
    - name: cpu-user
      resources:
      - name: "cpu"
        nominalQuota: 480
      - name: "memory"
        nominalQuota: 2000G"""
  if args.enable_pathways:
    return resources_yaml
  return ""


def get_kueue_covered_resources_config(args, cluster_hardware_name, resource_type, total_chips) -> str:
  """Gets Kueue covered resources configuration.

  Args:
    args: user provided arguments for running the command.
    cluster_hardware_name: cluster hardware name.
    resource_type: resource type of tpu or gpu.
    total_chips: total number of chips for the specific resource type.

  Returns:
    A string of Kueue covered resources configuration.
  """
  device_type = args.tpu_type if args.tpu_type else args.device_type
  if device_type == h100_device_type:
    config_format = '''
  - coveredResources: ["cpu", "memory", "{resource_type}"]
    flavors:
    - name: {cluster_hardware_name}
      resources:
      - name: "cpu"
        nominalQuota: {num_nodes}
      - name: "memory"
        nominalQuota: 150Mi
      - name: "{resource_type}"
        nominalQuota: {total_chips}'''
    config_string = config_format.format(
      cluster_hardware_name=cluster_hardware_name,
      resource_type=resource_type,
      total_chips=total_chips,
      num_nodes=args.num_nodes)
  else:
    config_format = '''
  - coveredResources: ["{resource_type}"]
    flavors:
    - name: {cluster_hardware_name}
      resources:
      - name: "{resource_type}"
        nominalQuota: {total_chips}
  '''

    config_string = config_format.format(
      cluster_hardware_name=cluster_hardware_name,
      resource_type=resource_type,
      total_chips=total_chips)
  return config_string



# TODO(vbarr): Remove this function when jobsets gets enabled by default on
# GKE clusters.
def set_jobset_on_cluster(args) -> int:
  """Add jobset command on server side and ask user to verify it is created.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
      'kubectl apply --server-side -f'
      ' https://github.com/kubernetes-sigs/jobset/releases/download/v0.4.0/manifests.yaml'
  )
  task = f'Install Jobset on {args.cluster}'
  return_code = run_command_with_updates_retry(command, task, args)

  if return_code != 0:
    xpk_print(f'{task} returned with ERROR {return_code}.\n')
    xpk_print(
      'This LIKELY means you\'re missing Kubernetes Permissions, you can'
      ' validate this by checking if the error references permission'
      ' problems such as `requires one of ["container.*"] permission(s)`.'
      ' Follow our readme: https://github.com/google/xpk/blob/main/README.md#troubleshooting'
      ' for instructions on how to fix these permissions.'
    )
  return return_code

def install_gpu_driver_on_cluster(args) -> int:
  """Install GPU driver on the cluster.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
      'kubectl apply -f'
      # pylint: disable=line-too-long
      'https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/master/nvidia-driver-installer/cos/daemonset-preloaded-latest.yaml'
  )
  return_code = run_command_with_updates(command, 'Install GPU Driver On Cluster', args)

  if return_code != 0:
    xpk_print(f'Install GPU Driver On Cluster request returned ERROR {return_code}')
    return 1

  return 0


def install_nccl_on_cluster(args) -> int:
  """Install NCCL plugin on the cluster.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
      'kubectl apply -f'
      # pylint: disable=line-too-long
      'https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/master/gpudirect-tcpx/nccl-tcpx-installer.yaml'
  )
  return_code = run_command_with_updates(command, 'Install NCCL Plugin On Cluster', args)

  if return_code != 0:
    xpk_print(f'Install NCCL Plugin On Cluster request returned ERROR {return_code}')
    return 1

  return 0


################### Subcommand Functions ###################
def default_subcommand_function(_args) -> int:  # args is unused, so pylint: disable=invalid-name
  """Default subcommand function.

  Args:
    _args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  xpk_print('Welcome to XPK! See below for overall commands:', flush=True)
  parser.print_help()
  cluster_parser.print_help()
  workload_parser.print_help()
  return 0


def cluster_create(args) -> int:
  """Function around cluster creation.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  system, return_code = get_system_characteristics(args)

  if return_code > 0:
    xpk_print('Fetching system characteristics failed!')
    xpk_exit(return_code)

  xpk_print(f'Starting cluster create for cluster {args.cluster}:', flush=True)
  add_zone_and_project(args)

  if _SERVICE_ACCOUNT_FEATURE_FLAG:
    service_account_name = get_service_account_name(args)
    service_account_exists = check_if_service_account_exists(args)
    if service_account_exists:
      xpk_print(f'Service Account: {service_account_name} already exist in the project.'
            ' Will not create a new service account.')
    else:
      # create a service account in the project
      create_service_account_code = create_service_account(args)
      if create_service_account_code != 0:
        xpk_exit(create_service_account_code)

    # add IAM roles to the service account
    add_roles_to_service_account_code = add_roles_to_service_account(args)
    if add_roles_to_service_account_code != 0:
      xpk_exit(add_roles_to_service_account_code)

  create_cluster_command_code = create_cluster_if_necessary(args)
  if create_cluster_command_code != 0:
    xpk_exit(create_cluster_command_code)

  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)

  device_type = args.tpu_type if args.tpu_type else args.device_type
  if device_type == h100_device_type:
    xpk_print('Setting up Network for cluster')
    set_up_cluster_network_code = set_up_cluster_network_for_a3(args)
    if set_up_cluster_network_code != 0:
      xpk_exit(set_up_cluster_network_code)

    xpk_print('Creating Network Config for cluster')
    create_cluster_network_config_code = create_cluster_network_config(args)
    if create_cluster_network_config_code != 0:
      xpk_exit(create_cluster_network_config_code)

  run_gke_node_pool_create_command_code = run_gke_node_pool_create_command(
      args, system
  )
  if run_gke_node_pool_create_command_code != 0:
    xpk_exit(run_gke_node_pool_create_command_code)

  xpk_print(
      'Enabling the jobset API on our cluster, to be deprecated when Jobset is'
      ' globally available'
  )
  set_jobset_on_cluster_code = set_jobset_on_cluster(args)
  if set_jobset_on_cluster_code != 0:
    xpk_exit(set_jobset_on_cluster_code)

  xpk_print('Enabling Kueue on the cluster')
  install_kueue_on_cluster_code = install_kueue_on_cluster(args)
  if install_kueue_on_cluster_code != 0:
    xpk_exit(install_kueue_on_cluster_code)

  xpk_print('Enable Kueue CRDs')
  enable_kueue_creds_code = enable_kueue_crds(args, system)
  if enable_kueue_creds_code != 0:
    xpk_exit(enable_kueue_creds_code)

  xpk_print('Creating ConfigMap for cluster')
  create_cluster_configmaps_code = create_cluster_configmaps(args, system)
  if create_cluster_configmaps_code != 0:
    xpk_exit(create_cluster_configmaps_code)

  if device_type == h100_device_type:
    xpk_print('Installing GPU Driver for cluster')
    install_gpu_driver_code = install_gpu_driver_on_cluster(args)
    if install_gpu_driver_code != 0:
      xpk_exit(install_gpu_driver_code)

    xpk_print('Installing NCCL Plugin for cluster')
    install_nccl_code = install_nccl_on_cluster(args)
    if install_nccl_code != 0:
      xpk_exit(install_nccl_code)

  xpk_print('GKE commands done! Resources are created.')
  xpk_print(
      'See your GKE Cluster here:'
      # pylint: disable=line-too-long
      f' https://console.cloud.google.com/kubernetes/clusters/details/{zone_to_region(args.zone)}/{args.cluster}/details?project={args.project}'
  )
  xpk_exit(0)


def cluster_delete(args) -> int:
  """Function around cluster delete.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  xpk_print(f'Starting cluster delete for cluster: {args.cluster}', flush=True)
  add_zone_and_project(args)
  run_gke_cluster_delete_command_code = run_gke_cluster_delete_command(args)
  if run_gke_cluster_delete_command_code != 0:
    xpk_exit(run_gke_cluster_delete_command_code)
  xpk_print(f'GKE commands done! Cluster {args.cluster} deleted.\n')
  return 0


def cluster_cacheimage(args) -> int:
  """Function around cluster cacheimage.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  xpk_print(
      f'Starting cluster cacheimage for cluster: {args.cluster}', flush=True
  )
  add_zone_and_project(args)

  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)
  system, return_code = get_system_characteristics(args)

  if return_code > 0:
    xpk_print('Fetching system characteristics failed!')
    xpk_exit(return_code)

  node_selector_key = AcceleratorTypeToAcceleratorCharacteristics[system.accelerator_type].accelerator_label
  yml_string = cluster_preheat_yml.format(
      cachekey=args.cache_key,
      image_name=args.docker_image,
      nodeSelectorKey=node_selector_key
  )
  tmp = write_temporary_file(yml_string)
  command_apply = f'kubectl apply -f {str(tmp.file.name)}'
  command_delete = (
      f'kubectl delete -f {str(tmp.file.name)} --ignore-not-found=true'
  )

  return_code = run_command_with_updates(
      command_delete, 'Deleting Cached Image', args
  )
  if return_code != 0:
    xpk_print(f'Delete Cached Image returned ERROR {return_code}')
    xpk_exit(return_code)

  return_code = run_command_with_updates(
      command_apply, 'Creating Cached Image', args
  )
  if return_code != 0:
    xpk_print(f'Create Cached Image returned ERROR {return_code}')
    xpk_exit(return_code)
  xpk_exit(0)


def cluster_describe(args) -> int:
  """Function around cluster describe.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  xpk_print(f'Starting nodepool list for cluster: {args.cluster}', flush=True)
  add_zone_and_project(args)

  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)

  command = (
      f'gcloud container node-pools  list --cluster {args.cluster} '
      f'--project={args.project} --region={zone_to_region(args.zone)}'
  )

  return_code = run_command_with_updates(command, 'Cluster nodepool list', args)
  if return_code != 0:
    xpk_exit(return_code)

  return_code_node_output, node_output = run_command_for_value(
      r"kubectl get node --no-headers=true --selector='cloud.google.com/gke-tpu-accelerator' | wc -l",
      'Count TPU Nodes',
      args,
  )
  if return_code_node_output != 0:
    xpk_exit(return_code_node_output)
  number_tpu_vms_in_cluster = int(node_output)

  return_code_pod_output, pod_output = run_command_for_value(
      "kubectl get pod -o=custom-columns='Status:.status.phase' | grep -i"
      ' Running | wc -l',
      'Count TPU Pods',
      args,
  )
  if return_code_pod_output != 0:
    xpk_exit(return_code_pod_output)
  number_tpu_pods_in_cluster = int(pod_output)

  xpk_print(
      f'The cluster contains {number_tpu_vms_in_cluster} TPUVMs of which'
      f' {number_tpu_pods_in_cluster} are in use.'
  )

  xpk_print('GKE commands done!\n')
  return 0


def cluster_list(args) -> int:
  """Function around cluster list.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  add_zone_and_project(args)
  xpk_print(f'For project {args.project} and zone {args.zone}:', flush=True)
  if run_gke_clusters_list_command(args):
    return 1
  return 0


def validate_docker_image(docker_image, args) -> int:
  """Validates that the user provided docker image exists in your project.

  Args:
    docker_image: The docker image to verify.
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """

  project = args.project

  if docker_image.find('gcr.io') == -1:
    return 0

  command = (
      f'gcloud container images describe {docker_image} --project {project}'
  )
  return_code = run_command_with_updates(
      command, 'Validate Docker Image', args, verbose=False
  )
  if return_code != 0:
    xpk_print(
        'Failed to validate your docker image, check the docker image. You'
        f' should be able to navigate to the URL {docker_image} in {project}'
    )
    return return_code
  else:
    return 0


def build_docker_image_from_base_image(args, verbose=True) -> tuple[int, str]:
  """Adds script dir to the base docker image and uploads the image.

  Args:
    args: user provided arguments for running the command.

  Returns:
    Tuple of:
      0 if successful and 1 otherwise.
      Name of the Docker image created.
  """

  # Pick a name for the docker image.
  docker_image_prefix = os.getenv('USER', 'unknown')
  docker_name = f'{docker_image_prefix}-runner'

  docker_file = script_dir_dockerfile.format(
      base_docker_image=args.base_docker_image,
  )
  tmp = write_temporary_file(docker_file)
  docker_build_command = (
      f'docker build -f {str(tmp.file.name)} -t {docker_name}'
      f' {args.script_dir}'
  )
  xpk_print(f'Building {args.script_dir} into docker image.')
  return_code = run_command_with_updates(
      docker_build_command, 'Building script_dir into docker image', args,
      verbose=verbose
  )
  if return_code != 0:
    xpk_print(
        'Failed to add script_dir to docker image, check the base docker image.'
        f' You should be able to navigate to the URL {args.base_docker_image}'
        f' in {args.project}.'
    )
    xpk_exit(1)

  # Pick a randomly generated `tag_length` character docker tag.
  tag_length = 4
  tag_random_prefix = ''.join(random.choices(string.ascii_lowercase, k=tag_length))
  tag_datetime = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
  tag_name = f'{tag_random_prefix}-{tag_datetime}'
  cloud_docker_image = f'gcr.io/{args.project}/{docker_name}:{tag_name}'
  xpk_print(f'Adding Docker Image: {cloud_docker_image} to {args.project}')

  # Tag the docker image.
  tag_docker_image_command = (
      f'docker tag {docker_name} {cloud_docker_image}'
  )
  return_code = run_command_with_updates(
      tag_docker_image_command, 'Tag Docker Image', args,
      verbose=verbose
  )
  if return_code != 0:
    xpk_print(
        f'Failed to tag docker image with tag: {tag_name}.'
        f' You should be able to navigate to the URL {cloud_docker_image} in'
        f' {args.project}.'
    )
    xpk_exit(1)

  # Upload image to Artifact Registry.
  upload_docker_image_command = (
      f'docker push {cloud_docker_image}'
  )
  return_code = run_command_with_updates(
      upload_docker_image_command, 'Upload Docker Image', args,
      verbose=verbose
  )
  if return_code != 0:
    xpk_print(
        f'Failed to upload docker image.'
        f' You should be able to navigate to the URL {cloud_docker_image} in'
        f' {args.project}.'
    )
    xpk_exit(1)
  return return_code, cloud_docker_image


def check_if_workload_exists(args) -> bool:
  """Check if workload exists.

  Args:
     args: user provided arguments for running the command.

  Returns:
    returns true if workload exist, otherwise returns false.
  """
  columns = {
      'Jobset': '.metadata.ownerReferences[0].name',
  }

  s = ','.join([key + ':' + value for key, value in columns.items()])

  command = f"kubectl get workloads -o=custom-columns='{s}'"
  return_code, return_msg = run_command_for_value(
      command, 'Check if Workload Already Exists', args
  )

  if return_code != 0:
    xpk_print(f'List Job request returned ERROR {return_code}')
    xpk_exit(return_code)

  lines = return_msg.split('\n')
  new_workload_name = args.workload
  for line in lines:
    if line == new_workload_name:
      return True
  return False


def check_if_workload_can_schedule(args, system):
  """Check if workload can schedule based on the cluster resources (tpu_type and maximum VM in cluster).

  Args:
    args: user provided arguments for running the command.
    system: system characteristics

  Returns:
    returns true if workload can schedule, otherwise returns false.
  """
  resources_configmap_name = f'{args.cluster}-{_CLUSTER_RESOURCES_CONFIGMAP}'
  cluster_config_map = get_cluster_configmap(args, resources_configmap_name)

  # Prevents workload creation failure for existing clusters with no ConfigMap
  if cluster_config_map is None:
    xpk_print(f'No ConfigMap exist for cluster with the name {resources_configmap_name}.')
    return True

  device_type = args.tpu_type if args.tpu_type else args.device_type
  if device_type not in cluster_config_map:
    xpk_print(f'{args.workload} is requesting {device_type} but '
      f'cluster only contains {cluster_config_map.keys()}. '
      'XPK will not create this workload.'
    )
    return False

  max_vm_in_cluster = cluster_config_map[device_type]
  if system.accelerator_type == AcceleratorType['GPU']:
    vm_required_by_workload = int(args.num_nodes)
  else:
    vm_required_by_workload = int(args.num_slices) * system.vms_per_slice
  if vm_required_by_workload > max_vm_in_cluster:
    xpk_print(
        f'{args.workload} is requesting {args.num_slices} slice/slices of {device_type}, '
        f'which is {vm_required_by_workload} VMs, '
        f'but the cluster only contains {max_vm_in_cluster} VMs of {device_type}. '
        'XPK will not create this workload.'
    )
    return False

  return True


def use_base_docker_image_or_docker_image(args) -> bool:
  """Checks for correct docker image arguments.

  Args:
    args: user provided arguments for running the command.

  Returns:
    True if intended to use base docker image, False to use docker image.
  """
  use_base_docker_image = True
  # Check if (base_docker_image and script_dir) or (docker_image) is set.
  if args.docker_image is not None:
    if args.script_dir is not default_script_dir:
      xpk_print(
          '`--script-dir` and --docker-image can not be used together. Please'
          ' see `--help` command for more details.'
      )
      xpk_exit(1)
    if args.base_docker_image is not default_docker_image:
      xpk_print(
          '`--base-docker-image` and --docker-image can not be used together.'
          ' Please see `--help` command for more details.'
      )
      xpk_exit(1)
    use_base_docker_image = False
  return use_base_docker_image


def setup_docker_image(args) -> tuple[int, str]:
  """Does steps to verify docker args, check image, and build image (if asked).

  Args:
    args: user provided arguments for running the command.

  Returns:
    tuple:
      0 if successful and 1 otherwise.
      Name of the docker image to use.
  """
  use_base_docker_image = use_base_docker_image_or_docker_image(args)

  docker_image = args.base_docker_image
  if use_base_docker_image:
    validate_docker_image_code = validate_docker_image(
        docker_image, args
    )
    if validate_docker_image_code != 0:
      xpk_exit(validate_docker_image_code)
    build_docker_image_code, docker_image = build_docker_image_from_base_image(args)
    if build_docker_image_code != 0:
      xpk_exit(build_docker_image_code)
  else:
    docker_image = args.docker_image
    validate_docker_image_code = validate_docker_image(
        args.docker_image, args
    )
    if validate_docker_image_code != 0:
      xpk_exit(validate_docker_image_code)

  return 0, docker_image

def get_main_and_sidecar_container(args, system, docker_image) -> str:
  """Generate yaml for main and sidecar container.
  Args:
    args: user provided arguments for running the command.
    system: system characteristics
    docker_image: docker image

  Returns:
    str:
      yaml for main and sidecar container
  """
  resource_type = AcceleratorTypeToAcceleratorCharacteristics[system.accelerator_type].resource_type
  main_container = get_main_container(args, system, docker_image, resource_type)
  yaml = """- name: stacktrace-explorer
                image: busybox:1.28
                args: [/bin/sh, -c, "while [ ! -d /tmp/debugging ]; do sleep 60; done; while [ ! -e /tmp/debugging/* ]; do sleep 60; done; tail -n+1 -f /tmp/debugging/*"]
                volumeMounts:
                - name: tpu-stack-trace
                  readOnly: true
                  mountPath: /tmp/debugging
              {main_container}
                volumeMounts:
                - name: tpu-stack-trace
                  mountPath: /tmp/debugging
              volumes:
              - name: tpu-stack-trace
  """
  return yaml.format(main_container=main_container)

def get_main_container(args, system, docker_image, resource_type) -> str:
  """Generate yaml for main container including the xpk command.
  Args:
    args: user provided arguments for running the command.
    system: system characteristics
    docker_image: docker image
    resource_type: The label to describe the resource type for TPUs/GPUs/CPUs.

  Returns:
    str:
      yaml for main container
  """

  xpk_internal_commands = ''
  gsutil_test_command = ''
  if args.debug_dump_gcs:
    gsutil_test_command = (
      'which gsutil >/dev/null 2>&1 || { echo >&2 "gsutil'
      ' is required but not installed. Aborting"; exit 24;};'
    )
    xpk_internal_commands += ('WORKER_ID=$HOSTNAME;'
                f'gsutil cp -r /tmp/xla_dump/ {args.debug_dump_gcs}/$WORKER_ID;')

  command = args.command
  if args.enable_debug_logs:
    command = ('TPU_STDERR_LOG_LEVEL=0 TPU_MIN_LOG_LEVEL=0 TF_CPP_MIN_LOG_LEVEL=0'
               f' TPU_VMODULE=real_program_continuator=1 {args.command}')

  yaml = """- name: {args.docker_name}
                image: {docker_image}
                {image_pull_policy}
                env: {env}
                ports:
                {container_ports}
                {jax_coordinator_port}
                securityContext:
                  privileged: true
                command:
                - bash
                - -c
                - |
                  echo XPK Start: $(date) ; _sigterm() ( kill -SIGTERM $! 2>/dev/null;); trap _sigterm SIGTERM;{gsutil_test_command}({command}) & PID=$!; while kill -0 $PID 2>/dev/null; do sleep 5; done; wait $PID; EXIT_CODE=$? ; {xpk_internal_commands} echo XPK End: $(date); echo EXIT_CODE=$EXIT_CODE; exit $EXIT_CODE
                resources:
                  limits:
                    {resources}
                volumeMounts:
                {pw_volume_mounts}
  """
  return yaml.format(args=args,
                   system=system,
                   image_pull_policy=add_image_pull_policy_for_pw(args),
                   env=get_env_container(args),
                   container_ports=add_container_ports(args),
                   jax_coordinator_port=add_jax_coordinator_port(system),
                   docker_image=docker_image,
                   gsutil_test_command=gsutil_test_command,
                   command=command,
                   xpk_internal_commands=xpk_internal_commands,
                   resources=get_main_container_resources(args, system, resource_type),
                   pw_volume_mounts=get_pw_volume_mounts(args))

def add_image_pull_policy_for_pw(args):
  """ Add image pull policy only for Pathways containers.
  Args:
    args: user provided args.

  Returns:
    str:
      YAML stating that the image will be pulled fro GCR every time.
  """
  yaml="""imagePullPolicy: Always"""
  if args.use_pathways:
    return yaml.format(args=args)
  return ""

def get_pw_volume_mounts(args) -> str:
  """ Resources for the main container.
  Args:
    args: user provided args.

  Returns:
    str:
      YAML for the volumes mounted within a Pathways container as a YAML string.
  """
  volume_yaml="""- mountPath: /tmp
                  name: shared-tmp"""
  if args.use_pathways:
    return volume_yaml
  return ""

def get_pathways_rm_args(args) -> str:
  """Arguments for the Pathways resource manager.
  Args:
    args: user provided arguments for running the command.

  Returns:
    str: yaml containing arguments for the Pathways resource manager.
  """
  yaml="""- --alsologtostderr
              - --pathways_server_port=38677
              - --pathways_server_provides_devices=false
              - --pathways_device_type=NONE
              - --pathways_persistent_compilation_cache=false
              - --pathways_compilation_mode=compile_at_worker
              - --pathways_tmp_dir_pattern={args.pathways_gcs_location}
              - --pathways_resource_manager_expected_num_worker_jobs={args.num_slices}"""
  if args.use_pathways:
    return yaml.format(args=args)
  else:
    return ""

def get_pathways_worker_args(args) -> str:
  """Arguments for the Pathways workers.
  Args:
    args: user provided arguments for running the command.

  Returns:
    str: yaml containing arguments for the Pathways workers.
  """
  yaml="""- --alsologtostderr
              - --pathways_server_port=38677
              - --pathways_resource_manager={args.workload}-rm-0-0.{args.workload}:38677
              - --pathways_persistent_compilation_cache=false
              - --pathways_compilation_mode=compile_at_worker
              - --xla_tpu_enable_data_parallel_all_reduce_opt=true
              - --xla_tpu_data_parallel_opt_different_sized_ops=true
              - --xla_tpu_enable_async_collective_fusion=true
              - --xla_tpu_enable_async_collective_fusion_fuse_all_gather=true
              - --xla_tpu_enable_async_collective_fusion_multiple_steps=true
              - --xla_tpu_overlap_compute_collective_tc=true
              - --xla_enable_async_all_gather=true
              - --pathways_tmp_dir_pattern={args.pathways_gcs_location}"""
  if args.use_pathways:
    return yaml.format(args=args)
  else:
    return ""

def get_pathways_proxy_args(args) -> str:
  """Arguments for the Pathways proxy.
  Args:
    args: user provided arguments for running the command.

  Returns:
    str: yaml containing arguments for the Pathways proxy.
  """
  yaml="""- --alsologtostderr
              - --v=0
              - --pathways_ifrt_proxy_server_resource_manager={args.workload}-rm-0-0.{args.workload}:38677
              - --pathways_ifrt_proxy_server_port=38676
              - --pathways_tmp_dir_pattern={args.pathways_gcs_location}
              - --pathways_xprof_trace_enable_bulk_upload=true
              - --pathways_plaque_network=gcp"""
  if args.use_pathways:
    return yaml.format(args=args)
  else:
    return ""

def get_env_container(args):
  """ Environment configuration for the main container.
  Args:
    args: user provided args.

  Returns:
    str:
      YAML with the env config for the main container, as a YAML string.
  """
  env_yaml="""
                - name: XCLOUD_ENVIRONMENT
                  value: GCP
                - name: JAX_PLATFORMS
                  value: proxy
                - name: JAX_BACKEND_TARGET
                  value: grpc://{args.workload}-proxy-0-0.{args.workload}:38676
                - name: JOBSET_NAME
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.annotations['jobset.sigs.k8s.io/jobset-name']"""
  if args.use_pathways:
    return env_yaml.format(args=args)
  return args.env

def get_main_container_resources(args, system, resource_type) -> str:
  """ Resources for the main container.
  Args:
    args: user provided args.
    system: system characteristics.
    resource_type: TPU / GPU / CPU

  Returns:
    str:
      Workload resources port as a YAML string
  """
  # Resources requirements for Pathways workload containers are known.
  resources_yaml="""cpu: "24"
                    memory: 100G"""
  if args.use_pathways:
    return resources_yaml
  return f'{resource_type}: {system.chips_per_vm}'

def add_container_ports(args) -> str:
  """ Add slice builder and megascale container ports,
  for non-pathways workloads.

  Args:
    args: user provided args.

  Returns:
    str:
      Pathways server port as a YAML string
  """
  port_yaml ="""- containerPort: 8471
                - containerPort: 8080"""
  if args.use_pathways:
    return ''
  return port_yaml

def add_jax_coordinator_port(system) -> str:
  """Add jax coordinator port only for CPUs

  Args:
    system: system characteristics.

  Returns:
    str:
      jax coordinator port as a YAML string
  """
  if system.accelerator_type == AcceleratorType['CPU']:
    return '- containerPort: 1234'
  return ''

def get_gke_dashboard(args, dashboard_filter):
  """Get the identifier of GKE dashboard deployed in the project.

  Args:
    args: user provided arguments for running the command.

  Returns:
    bool:
      True if 'gcloud monitoring dashboards list' returned an error or
      multiple dashboards with same filter exist in the project,
      False otherwise.
    str:
      identifier of dashboard if deployed in project,
      None otherwise.
  """
  command = (
      'gcloud monitoring dashboards list'
      f' --project={args.project} --filter="{dashboard_filter}" --format="value(name)" --verbosity=error'
  )

  return_code, return_value = run_command_for_value(command, 'GKE Dashboard List', args)

  if return_code != 0:
    xpk_print(f'GKE Dashboard List request returned ERROR {return_code}. '
              'If there is a permissions error, please check '
              'https://github.com/google/xpk/blob/main/README.md#roles-needed-based-on-permission-errors '
              'for possible solutions.')
    return True, None

  if not return_value:
    xpk_print(
        f'No dashboard with {dashboard_filter} found in the project:{args.project}.'
    )
    return False, return_value

  dashboards = return_value.strip().split('\n')
  if len(dashboards) > 1:
    xpk_print(
      f'Multiple dashboards with same {dashboard_filter} exist in the project:{args.project}. '
      'Delete all but one dashboard deployed using https://github.com/google/cloud-tpu-monitoring-debugging.'
    )
    return True, None

  if dashboards[0]:
    return False, dashboards[0].strip().split('/')[-1]

  return True, None

def get_gke_outlier_dashboard(args):
  """Get the identifier of GKE outlier dashboard deployed in the project.

  Args:
    args: user provided arguments for running the command.

  Returns:
    str:
      identifier of outlier dashboard if deployed in project,
      None otherwise.
  """
  outlier_dashboard_filter = "displayName:'GKE - TPU Monitoring Dashboard'"
  is_error, dashboard_id = get_gke_dashboard(args, outlier_dashboard_filter)

  # 'gcloud monitoring dashboards list' returned an error or multiple dashboards with same filter exist in the project
  if is_error:
    return None

  # 'gcloud monitoring dashboards list' succeeded but no dashboard for the filter exist in the project
  if not is_error and not dashboard_id:
    xpk_print(
        'Follow https://github.com/google/cloud-tpu-monitoring-debugging'
        ' to deploy monitoring dashboard to view statistics and outlier mode of GKE metrics.'
    )
    return None

  return dashboard_id

def get_gke_debugging_dashboard(args):
  """Get the identifier of GKE debugging dashboard deployed in the project.

  Args:
    args: user provided arguments for running the command.

  Returns:
    str:
      identifier of debugging dashboard if deployed in project,
      None otherwise.
  """
  debugging_dashboard_filter = "displayName:'GKE - TPU Logging Dashboard'"
  is_error, dashboard_id = get_gke_dashboard(args, debugging_dashboard_filter)

  # 'gcloud monitoring dashboards list' returned an error or multiple dashboards with same filter exist in the project
  if is_error:
    return None

  # 'gcloud monitoring dashboards list' succeeded but no dashboard for the filter exist in the project
  if not is_error and not dashboard_id:
    xpk_print(
        'Follow https://github.com/google/cloud-tpu-monitoring-debugging'
        ' to deploy debugging dashboard to view stack traces collected in Cloud Logging.'
    )
    return None

  return dashboard_id

def create_accelerator_label(accelerator_type, system) -> str:
  """Generates accelerator label.

  Args:
    accelerator_type: type of accelerator.
    system: system characteristics.

  Returns:
    The accelerator label.
  """
  if accelerator_type == AcceleratorType['CPU']:
    return ""
  return f"{AcceleratorTypeToAcceleratorCharacteristics[accelerator_type].accelerator_label}: {system.gke_accelerator}"

def create_machine_label(accelerator_type, system) -> str:
  """Generates machine label.

  Args:
    accelerator_type: type of accelerator.
    system: system characteristics.

  Returns:
    The machine label.
  """
  if accelerator_type == AcceleratorType['TPU']:
    return f"{AcceleratorTypeToAcceleratorCharacteristics[accelerator_type].machine_label}: {system.topology}"
  return ""

def calculate_process_count(num_slices, vms_per_slice) -> str:
  """ Calculates the total number of processes in the workload.
  Args:
    num_slices: Number of slices to be used in the workload.
    vms_per_slice: number of VMs in each slice.

  Returns:
    str: total number of processes.
  """
  num_processes = int(num_slices) * int(vms_per_slice)

  return f"{num_processes}"

def get_cpu_env(num_slices, system) -> str:
  """Generate environment variables for CPU nodepools
  Args:
    num_slices: Number of slices to be used in the workload.
    system: system characteristics

  Returns:
    str: yaml containing env variables
  """
  yaml = """env:
                - name: REPLICATED_JOB_NAME
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.annotations['jobset.sigs.k8s.io/replicatedjob-name']
                - name: JOBSET_NAME
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.annotations['jobset.sigs.k8s.io/jobset-name']
                - name: JAX_COORDINATOR_ADDRESS
                  value: "$(JOBSET_NAME)-$(REPLICATED_JOB_NAME)-0-0.$(JOBSET_NAME)"
                - name: JOB_INDEX
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.annotations['jobset.sigs.k8s.io/job-index']
                - name: JOB_COMPLETION_INDEX
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.annotations['batch.kubernetes.io/job-completion-index']
                - name: PROCESSES_IN_JOB
                  value: "{processes_in_job}"
                - name: JAX_PROCESS_COUNT
                  value: "{process_count}"
  """
  if system.accelerator_type == AcceleratorType['CPU']:
    return yaml.format(processes_in_job = system.vms_per_slice,
                      process_count=calculate_process_count(num_slices,system.vms_per_slice))
  return ""

def get_cpu_affinity(accelerator_type) -> str:
  """Generate affinity rules for CPU nodepools, so that workload pods are
  not scheduled on the default pool machines.
  Args:
    accelerator_type: TPU / GPU / CPU

  Returns:
    str: yaml containing affinity constraints
  """
  yaml = """affinity:
                nodeAffinity:
                  requiredDuringSchedulingIgnoredDuringExecution:
                    nodeSelectorTerms:
                    - matchExpressions:
                      - key: cloud.google.com/gke-nodepool
                        operator: NotIn
                        values:
                        - default-pool
"""
  if accelerator_type == AcceleratorType['CPU']:
    return yaml
  return ""

def get_system_characteristics(args) -> tuple[SystemCharacteristics|None, int]:
  """Get system characteristics based on user provided arguments.

  Args:
    args: user provided arguments for running the command.

  Returns:
    Tuple with string with the system characteristics and
    int of 0 if successful and 1 otherwise.
  """
  device_type = args.tpu_type if args.tpu_type else args.device_type
  if device_type in UserFacingNameToSystemCharacteristics:
    return UserFacingNameToSystemCharacteristics[device_type], 0
  else:
    return None, 1

def workload_create(args) -> int:
  """Run jobset apply command for a file.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  add_zone_and_project(args)

  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)

  workload_exists = check_if_workload_exists(args)

  if workload_exists:
    xpk_print(
    f'{args.workload} already exists, XPK will not create this workload.'
    ' Please pick a new workload name'
  )
    xpk_exit(1)

  xpk_print("Starting workload create", flush=True)
  system, return_code = get_system_characteristics(args)

  if return_code > 0:
    xpk_print("Fetching system characteristics failed!")
    xpk_exit(return_code)

  if not check_if_workload_can_schedule(args, system):
    xpk_exit(1)

  xpk_print("Starting workload create", flush=True)

  setup_docker_image_code, docker_image = setup_docker_image(args)
  if setup_docker_image_code != 0:
    xpk_exit(setup_docker_image_code)

  add_env_config(args)

  debugging_dashboard_id = None
  resource_type = AcceleratorTypeToAcceleratorCharacteristics[system.accelerator_type].resource_type
  if system.accelerator_type == AcceleratorType['TPU'] and args.deploy_stacktrace_sidecar:
    xpk_print('Sidecar container to display stack traces for TPU workloads will also be deployed.')
    container = get_main_and_sidecar_container(args, system, docker_image)
    # Get GKE debugging dashboard only when sidecar container is deployed for TPU workloads
    debugging_dashboard_id = get_gke_debugging_dashboard(args)
  elif system.accelerator_type in (AcceleratorType['CPU'], AcceleratorType['TPU']):
    container = get_main_container(args, system, docker_image, resource_type)

  if system.accelerator_type == AcceleratorType['GPU']:
    yml_string = gpu_workload_create_yaml.format(args=args,
                                                 docker_image=docker_image,
                                                 command=args.command,
                                                 accelerator_label=create_accelerator_label(system.accelerator_type, system),
                                                 machine_label=create_machine_label(system.accelerator_type, system),
                                                 node_pool_name=f'{args.cluster}-np-0',
                                                 chips_per_vm=system.chips_per_vm)
  elif args.use_pathways:
    # Ensure the cluster and CPU nodepools were created with --enable-pathways
    all_node_pools = get_all_nodepools_programmatic(args)
    desired_pw_cpu_node_pools = {'cpu-user-np', 'cpu-rm-np', 'cpu-proxy-np'}
    if not desired_pw_cpu_node_pools.issubset(set(all_node_pools[0])):
      xpk_print(
          'Cluster needs to be created with --enable-pathways to run Pathways workloads.'
      )
      xpk_exit(1)

    # Ensure device type is TPUs - currently Pathways supports TPUs only.
    if system.accelerator_type != AcceleratorType['TPU']:
      xpk_print(
          'Currently, Pathways workloads can only be run on TPUs.'
      )
      xpk_exit(1)

    yml_string = pw_workload_create_yaml.format(args=args,
                                        system=system,
                                        container=container,
                                        accelerator_label=create_accelerator_label(system.accelerator_type, system),
                                        machine_label=create_machine_label(system.accelerator_type, system),
                                        pathways_rm_args = get_pathways_rm_args(args),
                                        pathways_worker_args = get_pathways_worker_args(args),
                                        pathways_proxy_args = get_pathways_proxy_args(args),
                                        resource_type=resource_type,
                                        local_queue_name=_LOCAL_QUEUE_NAME)
  else:
    yml_string = workload_create_yaml.format(args=args,
                                      system=system,
                                      container=container,
                                      affinity=get_cpu_affinity(system.accelerator_type),
                                      env=get_cpu_env(args.num_slices,system),
                                      accelerator_label=create_accelerator_label(system.accelerator_type, system),
                                      machine_label=create_machine_label(system.accelerator_type, system),
                                      local_queue_name=_LOCAL_QUEUE_NAME)
  tmp = write_temporary_file(yml_string)
  command = f'kubectl apply -f {str(tmp.file.name)}'
  return_code = run_command_with_updates(command, 'Creating Workload', args)

  if return_code != 0:
    xpk_print(f'Create Workload request returned ERROR {return_code}')
    xpk_exit(return_code)

  # Get GKE outlier dashboard for TPU
  outlier_dashboard_id = None
  if system.accelerator_type == AcceleratorType['TPU']:
    outlier_dashboard_id = get_gke_outlier_dashboard(args)

  if args.use_pathways:
    xpk_print(
    'Follow your Pathways workload here:'
    # pylint: disable=line-too-long
    f' https://console.cloud.google.com/kubernetes/job/{zone_to_region(args.zone)}/{args.cluster}/default/{args.workload}-main-0/details?project={args.project}'
    )
  else:
    xpk_print(
      'Follow your workload here:'
      # pylint: disable=line-too-long
      f' https://console.cloud.google.com/kubernetes/service/{zone_to_region(args.zone)}/{args.cluster}/default/{args.workload}/details?project={args.project}'
    )

  if outlier_dashboard_id is not None:
    xpk_print(
    'Check statistics and outlier mode of GKE metrics here:'
    # pylint: disable=line-too-long
    f' https://console.cloud.google.com/monitoring/dashboards/builder/{outlier_dashboard_id}?project={args.project}&f.rlabel.cluster_name.ClusterName={args.cluster}.'
    f' To view the metric data for your workload, select {args.workload} from the JobName filter on the dashboard.'
  )

  if debugging_dashboard_id is not None:
    xpk_print(
    'Check stack traces collected in Cloud Logging here:'
    # pylint: disable=line-too-long
    f' https://console.cloud.google.com/monitoring/dashboards/builder/{debugging_dashboard_id}?project={args.project}&f.rlabel.cluster_name.ClusterName={args.cluster}.'
    f' To view the stack traces for your workload, select {args.workload} from the JobName filter on the dashboard.'
  )

  xpk_exit(0)


def workload_delete(args) -> int:
  """Function around workload delete.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  xpk_print('Starting Workload delete', flush=True)
  add_zone_and_project(args)
  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)

  will_delete = True
  if not args.workload:
    xpk_print("Get the name of the workloads in the cluster.")
    return_code, return_value = get_workload_list(args)

    if return_code != 0:
      xpk_print(f'List Job request returned ERROR {return_code}')
      xpk_exit(return_code)
    # Skip the header
    workloads = [x.split(' ')[0] for x in return_value.splitlines()][1:]
    if workloads and not args.force:
      will_delete = get_user_input(
        f'Planning to delete {len(workloads)} workloads in the cluster {args.cluster} '
        f'including {workloads}. \nDo you wish to delete: y (yes) / n (no):\n')
  else:
    workloads = [args.workload]

  if not workloads:
    xpk_print("There are no workloads to delete matching the filter in the cluster.")
  elif not will_delete:
    xpk_print("Skipping delete command.")
  else:
    commands = []
    task_names = []
    for workload in workloads:
      args.workload = workload
      command = f'kubectl delete jobset {workload} -n default'
      task_name = f'WorkloadDelete-{workload}'
      commands.append(command)
      task_names.append(task_name)

    # Not batching deletion for single workload
    if len(workloads) == 1:
      return_code = run_command_with_updates(commands[0], 'Delete Workload', args)
    else:
      return_code = run_commands(commands, 'Delete Workload', task_names, batch=100)

    if return_code != 0:
      xpk_print(f'Delete Workload request returned ERROR {return_code}')
      xpk_exit(return_code)
  xpk_exit(0)


def workload_list_awk_command(filter_key) -> str:
  """Function returns the awk command needed from the filter specified.

  Args:
    filter_key: workload list filter to awk against

  Returns:
    awk command to use in filtering workload list.
  """

  return f' | awk -e \'NR == 1 || {filter_key} {{print $0}}\''


def determine_workload_list_filter_by_status(args) -> str:
  """Function to create the filtered view of workload list.

  Args:
    args: user provided arguments for running the command.

  Returns:
    the argument needed to filter by status of jobs in workload list.
  """
  # Argument positions related to columns created by workload list command.
  status_arg='$7'
  running_vms_arg='$5'
  status_verbose_arg='$9'
  if args.filter_by_status == 'EVERYTHING':
    return ''
  elif args.filter_by_status == 'RUNNING':
    # Running includes the status Admitted or Evicted, and when the number of
    # vms running is > 0.
    return workload_list_awk_command(
        f'({status_arg} ~ \"Admitted|Evicted\" && {running_vms_arg} ~ /^[0-9]+$/ && {running_vms_arg} > 0)'
    )
  elif args.filter_by_status == 'QUEUED':
    # Queued includes the status Admitted or Evicted, and when the number of
    # vms running is 0.
    return workload_list_awk_command(
        f'({status_arg} ~ \"Admitted|Evicted|QuotaReserved\" && ({running_vms_arg} ~ \"<none>\" || {running_vms_arg} == 0))'
    )
  elif args.filter_by_status == 'FINISHED':
    return workload_list_awk_command(f'{status_arg} == \"Finished\"')
  elif args.filter_by_status == 'FAILED':
    # Failed includes the status Finished, and when the verbose reason is failed.
    return workload_list_awk_command(f'({status_arg} == \"Finished\" && {status_verbose_arg} ~ \"failed\")')
  elif args.filter_by_status == 'SUCCESSFUL':
    # Failed includes the status Finished, and when the verbose reason is finished/success.
    return workload_list_awk_command(f'({status_arg} == \"Finished\" && {status_verbose_arg} ~ \"finished\")')
  raise RuntimeError(f'Can not find filter type: {args.filter_by_status}')


def determine_workload_list_filter_by_job(args) -> str:
  """Function to filter view of workload list based on job name.

  Args:
    args: user provided arguments for running the command.

  Returns:
    the argument needed to filter job names from workload list
  """
  # Argument positions related to columns created by workload list command.
  if not args.filter_by_job:
    return ''
  else:
    job_name_arg="$1"
    return workload_list_awk_command(f'{job_name_arg} ~ \"{args.filter_by_job}\"')


def get_workload_list(args) -> tuple[int, str]:
  """Function to get the list of the workloads in the cluster.

  Args:
    args: user provided arguments for running the command.

  Returns:
    return_code: 0 if successful and 1 otherwise.
    return_value: workloads in the cluster matching the criteria.
  """
  columns = {
      'Jobset Name': '.metadata.ownerReferences[0].name',
      'Created Time': '.metadata.creationTimestamp',
      'Priority': '.spec.priorityClassName',
      'TPU VMs Needed': '.spec.podSets[0].count',
      'TPU VMs Running/Ran': '.status.admission.podSetAssignments[-1].count',
      'TPU VMs Done': '.status.reclaimablePods[0].count',
      'Status': '.status.conditions[-1].type',
      'Status Message': '.status.conditions[-1].message',
      'Status Time': '.status.conditions[-1].lastTransitionTime',
    }
  s = ','.join([key + ':' + value for key, value in columns.items()])

  workload_list_filter_status_cmd = determine_workload_list_filter_by_status(args)
  workload_list_filter_job_cmd = determine_workload_list_filter_by_job(args)
  command = (f'kubectl get workloads -o=custom-columns="{s}" '
             f'{workload_list_filter_status_cmd} {workload_list_filter_job_cmd}'
             )

  return_code, return_value = run_command_for_value(
    command, f'List Jobs with filter-by-status={args.filter_by_status}'
    f' with filter-by-jobs={args.filter_by_job}', args)
  return return_code, return_value


def workload_list(args) -> int:
  """Function around workload list.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  xpk_print(args)

  xpk_print('Starting workload list', flush=True)
  add_zone_and_project(args)
  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)

  return_code, return_value = get_workload_list(args)

  if return_code != 0:
    xpk_print(f'List Job request returned ERROR {return_code}')
    xpk_exit(return_code)
  xpk_print(return_value)
  xpk_exit(0)


def inspector_run_command_helper(args, command, command_description, file) -> int:
  """Runs a command for xpk inspector, and build the output file.

  Args:
    args: user provided arguments for running the command.
    command: the cli command to run.
    command_description: a brief description of the command run.
    file: file to add command output to.

  Returns:
    0 if successful and 1 otherwise.
  """
  prefix = f'Command: {command}\nCommand Description: {command_description}\n'
  postfix = '========================================================'
  return_code, command_output = run_command_for_value(command, f'{command_description}', args)

  if return_code != 0:
    xpk_print(f'{command} returned ERROR {return_code} with output: {command_output}')
    return 1

  inspector_command_output = f'{prefix} \n{command_output} \n{postfix} \n'
  append_temporary_file(inspector_command_output, file)

  if args.print_to_terminal:
    xpk_print(inspector_command_output)
  return 0


def inspector_run_workload_list_helper(args, command_description, file) -> int:
  """Runs a workload list command for xpk inspector, and build the output file.

  Args:
    args: user provided arguments for running the command.
    command_description: a brief description of the command run.
    file: file to add command output to.

  Returns:
    0 if successful and 1 otherwise.
  """
  prefix = f'Command Description: {command_description}\n'
  postfix = '========================================================'
  return_code, command_output = get_workload_list(args)
  if return_code != 0:
    xpk_exit(return_code)
  inspector_command_output = f'{prefix} \n{command_output} \n{postfix} \n'
  append_temporary_file(inspector_command_output, file)
  if args.print_to_terminal:
    xpk_print(inspector_command_output)
  return 0


def inspector_output_link_helper(args, link, link_description, file) -> int:
  """Outputs a link for xpk inspector to the output file.

  Args:
    args: user provided arguments for.
    link: link to output.
    link_description: describes what the link is for.
    file: file to add command output to.

  Returns:
    0 if successful and 1 otherwise.
  """
  inspector_link = (
    f'Link Description: {link_description}\n'
    f'Link: {link}\n'
    '========================================================'
  )
  append_temporary_file(inspector_link, file)
  if args.print_to_terminal:
    xpk_print(inspector_link)
  return 0


def inspector(args) -> int:
  """Function around inspector which investigates failures in the kueue.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  # Future Improvements for inspector:
  # 1. Print out the resource flavor.
  # 2. List what is next in Queue.
  # 3. Split inspector into different subcommands to parse info easier.

  final_return_code = 0
  xpk_print(args)

  add_zone_and_project(args)
  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)

  inspector_file = write_temporary_file("==================\nXPK inspector OUTPUT:\n==================\n")
  command_and_descriptions = [
    ('gcloud version', 'Local Setup: gcloud version'),
    ('gcloud config get project; gcloud config get compute/zone; gcloud config get compute/region',
     'Local Setup: Project / Zone / Region'),
    (f'gcloud beta container clusters list --project {args.project} --region {zone_to_region(args.zone)}'
     f' | grep -e NAME -e {args.cluster}','GKE: Cluster Details'),
    (f'kubectl get configmap {args.cluster}-{_CLUSTER_METADATA_CONFIGMAP} -o yaml',
     'GKE: Cluster Metadata ConfigMap Details'),
    (f'kubectl get configmap {args.cluster}-{_CLUSTER_RESOURCES_CONFIGMAP} -o yaml',
     'GKE: Cluster Resources ConfigMap Details'),
    (f'gcloud beta container node-pools list --cluster {args.cluster}  --project={args.project} '
     f'--region={zone_to_region(args.zone)}', 'GKE: Node pool Details'),
    ('kubectl get node -o custom-columns=\'NODE_NAME:metadata.name,'
     ' READY_STATUS:.status.conditions[?(@.type=="Ready")].status,'
     ' NODEPOOL:metadata.labels.cloud\\.google\\.com/gke-nodepool\'', 'Kubectl: All Nodes'),
    ('kubectl get node -o custom-columns=\':metadata.labels.cloud\\.google\\.com/gke-nodepool\''
     ' | sort | uniq -c', 'Kubectl: Number of Nodes per Node Pool'),
    ('kubectl get node -o custom-columns=\'NODE_NAME:metadata.name,'
     ' READY_STATUS:.status.conditions[?(@.type=="Ready")].status,'
     ' NODEPOOL:metadata.labels.cloud\\.google\\.com/gke-nodepool\' | grep -w True | awk {\'print $3\'} | sort | uniq -c',
     'Kubectl: Healthy Node Count Per Node Pool'),
    (f'kubectl describe ClusterQueue {_CLUSTER_QUEUE_NAME}', 'Kueue: ClusterQueue Details'),
    (f'kubectl describe LocalQueue {_LOCAL_QUEUE_NAME}', 'Kueue: LocalQueue Details'),
    ('kubectl describe Deployment kueue-controller-manager -n kueue-system', 'Kueue: Kueue Deployment Details'),
    ('kubectl describe Deployment jobset-controller-manager -n jobset-system', 'Jobset: Deployment Details'),
    ('kubectl logs deployment/kueue-controller-manager -n kueue-system --tail=100 --prefix=True', 'Kueue Manager Logs'),
    ('kubectl logs deployment/jobset-controller-manager -n jobset-system --tail=100 --prefix=True', 'Jobset Manager Logs'),
  ]

  for command, description in command_and_descriptions:
    return_code = inspector_run_command_helper(args, command, description, inspector_file)
    if return_code != 0:
      final_return_code = return_code
      xpk_print(f'inspector failed in command: {command} description: {description} return code: {return_code}')

  # Workload list views:
  filter_by_statuses = ['EVERYTHING','QUEUED','RUNNING']
  for filter_by_status in filter_by_statuses:
    args.filter_by_job=None
    args.filter_by_status=filter_by_status
    command_description = (
      f'xpk workload list --filter-by-status={args.filter_by_status}'
      f' --filter-by-job={args.filter_by_job} --project={args.project} --zone={args.zone}'
      f' --cluster={args.cluster}'
    )
    return_code = inspector_run_workload_list_helper(args, command_description, inspector_file)
    if return_code != 0:
      final_return_code = return_code
      xpk_print(f'inspector failed in description: {command_description} return code: {return_code}')

  # If a workload argument is provided, list out workload specific details.
  if args.workload:
    xpk_print(args.workload)
    args.filter_by_job=args.workload
    args.filter_by_status='EVERYTHING'
    command_description = (
      f'xpk workload list --filter-by-status={args.filter_by_status}'
      f' --filter-by-job={args.filter_by_job} --project={args.project} --zone={args.zone}'
      f' --cluster={args.cluster}'
    )
    return_code = inspector_run_workload_list_helper(args, command_description, inspector_file)
    if return_code != 0:
      final_return_code = return_code
      xpk_print(f'inspector failed in description: {command_description} return code: {return_code}')

    command = f'kubectl describe jobsets {args.workload}'
    command_description = f'Jobset config for {args.workload}'
    return_code = inspector_run_command_helper(args, command, command_description, inspector_file)
    if return_code != 0:
      final_return_code = return_code
      xpk_print(f'inspector failed in command: {command} description: {command_description} return code: {return_code}')

    command = f'kubectl describe workloads jobset-{args.workload}'
    command_description = f'Workload config for {args.workload}'
    return_code = inspector_run_command_helper(args, command, command_description, inspector_file)
    if return_code != 0:
      final_return_code = return_code
      xpk_print(f'inspector failed in command: {command} description: {command_description} return code: {return_code}')

  # Cloud Console Links:
  workload_links = []
  if args.workload:
    workload_links = [
      (f'Cloud Console for the workload {args.workload}',
      # pylint: disable=line-too-long
       f'https://console.cloud.google.com/kubernetes/service/{zone_to_region(args.zone)}/{args.cluster}/default/{args.workload}/details?project={args.project}')
    ]

  links = [
    ('Cloud Console for the GKE Cluster',
      # pylint: disable=line-too-long
     f'https://console.cloud.google.com/kubernetes/clusters/details/{zone_to_region(args.zone)}/{args.cluster}/details?project={args.project}'),
    ('Cloud Console for all workloads in GKE Cluster',
      # pylint: disable=line-too-long
     f'https://console.cloud.google.com/kubernetes/workload/overview?project={args.project}&pageState=((gke%2F{zone_to_region(args.zone)}%2F{args.cluster}))'),
    ('Cloud Console for IAM Permissions', f'https://console.cloud.google.com/iam-admin/iam?project={args.project}'),
    ('Cloud Console for Quotas', f'https://console.cloud.google.com/iam-admin/quotas?project={args.project}'),
  ]
  links.extend(workload_links)

  for description, workload_link in links:
    return_code = inspector_output_link_helper(args, workload_link, description, inspector_file)
    if return_code != 0:
      final_return_code = return_code
      xpk_print(f'inspector failed in link: {workload_link} description: {description} return code: {return_code}')

  # Summarize inspector:
  xpk_print(f'Find xpk inspector output file: {inspector_file.name}')

  if final_return_code != 0:
    xpk_print(
      'Something was unable to run in xpk inspector, please look through the output'
      f' as it may clue to the failure reason. Return Code: {final_return_code}'
    )
  xpk_exit(final_return_code)


def add_shared_arguments(custom_parser):
  """Add shared arguments to the parser.

  Args:
    custom_parser: parser to add shared arguments to.
  """
  custom_parser.add_argument(
      '--project',
      type=str,
      default=None,
      help='GCE project name, defaults to "gcloud config project."',
  )
  custom_parser.add_argument(
      '--zone',
      type=str,
      default=None,
      help=(
          'GCE zone, e.g. us-central2-b, defaults to "gcloud config '
          'compute/zone." Only one of --zone or --region is allowed in a '
          'command.'
      ),
  )
  custom_parser.add_argument(
      '--dry-run',
      type=bool,
      action=argparse.BooleanOptionalAction,
      default=False,
      help=(
          'If given `--dry-run`, xpk will print the commands it wants to run'
          ' but not run them. This is imperfect in cases where xpk might'
          ' branch based on the output of commands'
      ),
  )


############### Define flags ###############
# Create top level parser for xpk command.
parser = argparse.ArgumentParser(description='xpk command', prog='xpk')

xpk_subcommands = parser.add_subparsers(
    title='xpk subcommands', dest='xpk_subcommands', help='Top level commands'
)
parser.set_defaults(func=default_subcommand_function)


def workload_name_type(value, pat=re.compile(r'[a-z]([-a-z0-9]*[a-z0-9])?')):
  """Validate that the workload name matches the expected pattern."""
  match = pat.fullmatch(value)
  if not match or len(match.group(0)) > 40:
    raise argparse.ArgumentTypeError(
        'Workload name must be less than 40 characters and match the pattern'
        f' `{pat.pattern}`'
        f' Name is currently {value}'
    )
  return value


def directory_path_type(value):
  if not os.path.isdir(value):
    raise argparse.ArgumentTypeError(
      f'Directory path is invalid. User provided path was {value}'
    )
  return value


#### "cluster" command parser. ####
cluster_parser = xpk_subcommands.add_parser(
    'cluster',
    help='Commands around creating, deleting, and viewing clusters.',
)
cluster_parser.set_defaults(func=default_subcommand_function)
cluster_subcommands = cluster_parser.add_subparsers(
    title='cluster subcommands',
    dest='xpk_cluster_subcommands',
    help=(
        'These are commands related to cluster management. Look at help for'
        ' specific subcommands for more details.'
    ),
)

### "cluster create" command parser ###
cluster_create_parser = cluster_subcommands.add_parser(
    'create', help='Create cloud clusters.'
)
cluster_create_required_arguments = cluster_create_parser.add_argument_group(
    'Required Arguments',
    'Arguments required for cluster create.',
)
cluster_create_optional_arguments = cluster_create_parser.add_argument_group(
    'Optional Arguments', 'Arguments optional for cluster create.'
)
cluster_create_capacity_arguments = cluster_create_parser.add_argument_group(
    'Capacity Arguments', 'Arguments related to capacity for cluster create.'
)

### Required arguments.
cluster_create_required_arguments.add_argument(
    '--cluster',
    type=str,
    default=None,
    help=(
        'The name of the cluster. Will be used as the prefix for internal'
        ' objects in the cluster.'
    ),
    required=True,
)

cluster_device_group = cluster_create_required_arguments.add_mutually_exclusive_group(required=True)

cluster_device_group.add_argument(
    '--tpu-type',
    type=str,
    default=None,
    help='The tpu type to use, v5litepod-16, etc.'
)
cluster_device_group.add_argument(
    '--device-type',
    type=str,
    default=None,
    help='The device type to use (can be tpu or gpu or cpu), v5litepod-16, h100-80gb-8, n2-standard-32-4 etc.'
)


# Capacity Arguments
cluster_create_capacity_arguments.add_argument(
    '--on-demand',
    action='store_true',
    help=(
        'Sets node pool creation to use on-demand resources. '
        ' See `--reservation` or `--spot` for other capacity types.'
    ),
)
cluster_create_capacity_arguments.add_argument(
    '--reservation',
    type=str,
    help=(
        'The reservation to be used for acquiring resources in the'
        ' cluster. This will attempt to find the provided reservation.'
        ' See `--spot` or `--on-demand` for other capacity types.'
    ),
)
cluster_create_capacity_arguments.add_argument(
    '--spot',
    action='store_true',
    help=(
        'Sets node pool creation to use spot resources.'
        ' See `--reservation` or `--on-demand` for other capacity types.'
    ),
)

### Optional Arguments
cluster_create_optional_arguments.add_argument(
    '--host-maintenance-interval',
    type=str,
    choices=['AS_NEEDED', 'PERIODIC'],
    default='AS_NEEDED',
    help='The maintenance policy of the cluster and respective clusters.',
)
cluster_create_optional_arguments.add_argument(
    '--gke-version',
    type=str,
    default=default_gke_version,
    help=(
        'The GKE version of the cluster and respective clusters. The default is'
        f' "{default_gke_version}".'
    ),
)
cluster_create_optional_arguments.add_argument(
    '--num-slices',
    type=int,
    default=1,
    help='The number of slices to run the job on, defaults to 1.',
    required=False,
)
cluster_create_optional_arguments.add_argument(
    '--num-nodes',
    type=int,
    default=2,
    help='The number of nodes for a cluster, defaults to 2.',
    required=False,
)
cluster_create_optional_arguments.add_argument(
    '--enable-pathways',
    action='store_true',
    help='Enable cluster to accept Pathways workloads.',
)
cluster_create_optional_arguments.add_argument(
    '--pathways-gce-machine-type',
    type=str,
    default="n1-standard-32",
    help='The CPU type for Pathways CPU nodepools'
)
cluster_create_optional_arguments.add_argument(
  '--default-pool-cpu-machine-type',
    type=str,
    default='e2-standard-16',
    help=(
      'Set the machine type within the default cpu node pool. For'
      ' regional clusters, all zones must support the machine type.'
    )
)
cluster_create_optional_arguments.add_argument(
  '--cluster-cpu-machine-type',
    type=str,
    default='',
    help=(
      'Getting deprecated soon! Please use --default-pool-cpu-machine-type'
      'instead, to denote the machine type of the default cpu node pool. Set'
      ' the machine type of other cpu nodepools using --device-type.'
    )
)
cluster_create_optional_arguments.add_argument(
    '--custom-cluster-arguments',
    type=str,
    default='',
    help=(
        'Users can add their own arguments to customize their cluster create'
        ' command. Do note, these will not override already used cluster'
        ' creation arguments.'
        " e.g. --custom-cluster-arguments='--network=mtu9k --subnetwork=mtu9k'"
    ),
)
cluster_create_optional_arguments.add_argument(
    '--custom-tpu-nodepool-arguments',
    type=str,
    default='',
    help=(
        'Users can add their own arguments to customize their tpu node pool'
        ' create command. Do note, these will not override already used node'
        ' pool creation arguments. e.g.'
        ' --custom-tpu-nodepool-arguments="--enable-ip-alias"'
    ),
)
cluster_create_optional_arguments.add_argument(
    '--force',
    action='store_true',
    help=(
      'Forces node pool creation and delete commands to run without additional'
      ' approval.'
    ),
)
add_shared_arguments(cluster_create_optional_arguments)

cluster_create_parser.set_defaults(func=cluster_create)

### "cluster delete" command parser ###
cluster_delete_parser = cluster_subcommands.add_parser(
    'delete',
    help='Delete cloud clusters.',
)
cluster_delete_required_arguments = cluster_delete_parser.add_argument_group(
    'Required Arguments',
    'Arguments required for cluster delete.',
)
cluster_delete_optional_arguments = cluster_delete_parser.add_argument_group(
    'Optional Arguments', 'Arguments optional for cluster delete.'
)

### Required arguments
cluster_delete_required_arguments.add_argument(
    '--cluster',
    type=str,
    default=None,
    help='The name of the cluster to be deleted.',
    required=True,
)

### Optional Arguments
add_shared_arguments(cluster_delete_optional_arguments)
cluster_delete_parser.set_defaults(func=cluster_delete)

### "cluster cacheimage" command parser ###
cluster_cacheimage_parser = cluster_subcommands.add_parser(
    'cacheimage',
    help='Cache image.',
)
cluster_cacheimage_required_arguments = (
    cluster_cacheimage_parser.add_argument_group(
        'Required Arguments',
        'Arguments required for cluster cacheimage.',
    )
)
cluster_cacheimage_optional_arguments = (
    cluster_cacheimage_parser.add_argument_group(
        'Optional Arguments', 'Arguments optional for cluster cacheimage.'
    )
)
cluster_cacheimage_group = cluster_cacheimage_parser.add_mutually_exclusive_group(required=True)

### Device Type Argument
cluster_cacheimage_group.add_argument(
    '--tpu-type',
    type=str,
    default=None,
    help='The tpu type to cache images on, v5litepod-16, etc.'
)
cluster_cacheimage_group.add_argument(
    '--device-type',
    type=str,
    default=None,
    help='The device type to cache images on (can be tpu or gpu), v5litepod-16, h100-80gb-8, etc.'
)

### Required arguments
cluster_cacheimage_required_arguments.add_argument(
    '--cluster',
    type=str,
    default=None,
    help='The name of the cluster to cache the image.',
    required=True,
)
cluster_cacheimage_required_arguments.add_argument(
    '--docker-image',
    type=str,
    default=None,
    help='The docker-image to cache.',
    required=True,
)

### Optional Arguments
add_shared_arguments(cluster_cacheimage_optional_arguments)
cluster_cacheimage_optional_arguments.add_argument(
    '--cache-key',
    type=str,
    default='containerimage',
    help='The key to cache the docker image under.',
    required=False,
)
cluster_cacheimage_parser.set_defaults(func=cluster_cacheimage)

### "cluster describe" command parser ###
cluster_describe_parser = cluster_subcommands.add_parser(
    'describe',
    help='Describe a cluster.',
)
cluster_describe_required_arguments = (
    cluster_describe_parser.add_argument_group(
        'Required Arguments',
        'Arguments required for cluster describe.',
    )
)
cluster_describe_optional_arguments = (
    cluster_describe_parser.add_argument_group(
        'Optional Arguments', 'Arguments optional for cluster describe.'
    )
)

### Required arguments
cluster_describe_required_arguments.add_argument(
    '--cluster',
    type=str,
    default=None,
    help='The name of the cluster to be describe.',
    required=True,
)
### Optional Arguments
add_shared_arguments(cluster_describe_optional_arguments)


cluster_describe_parser.set_defaults(func=cluster_describe)

# "cluster list" command parser.
cluster_list_parser = cluster_subcommands.add_parser(
    'list', help='List cloud clusters.'
)
cluster_list_optional_arguments = cluster_list_parser.add_argument_group(
    'Optional Arguments', 'Arguments optional for cluster list.'
)
### Optional Arguments
add_shared_arguments(cluster_list_optional_arguments)


cluster_list_parser.set_defaults(func=cluster_list)

#### "workload" command parser. ####
workload_parser = xpk_subcommands.add_parser(
    'workload', help='commands around workload management'
)

workload_parser.set_defaults(func=default_subcommand_function)
workload_subcommands = workload_parser.add_subparsers(
    title='workload subcommands',
    dest='xpk_workload_subcommands',
    help='`create`, `list` and `delete` workloads on clusters',
)

# "workload create" command parser.
workload_create_parser = workload_subcommands.add_parser(
    'create', help='Create a new job.'
)
workload_create_parser_required_arguments = (
    workload_create_parser.add_argument_group(
        'Workload Built-in Arguments',
        'Configure xpk to create a Workload for you.'
    )
)
workload_create_parser_optional_arguments = (
    workload_create_parser.add_argument_group(
        'Optional Arguments', 'Arguments optional for `workload create`.'
    )
)
workload_base_docker_image_arguments = (
    workload_create_parser.add_argument_group(
        'Base Docker Image Arguments',
        'User supplies a base image or by default the image is set by xpk.'
        ' Xpk will add the `script_dir` to the base image creating an anonymous'
        ' docker image. These arguments are exclusive to `--docker-image`.'
    )
)
workload_docker_image_arguments = (
    workload_create_parser.add_argument_group(
        'Docker Image Arguments',
        '`--base-docker-image` is used by default. Set this argument if the'
        ' user wants the docker image to be used directly by the xpk workload.'
    )
)
workload_pathways_workload_arguments = (
    workload_create_parser.add_argument_group(
        'Pathways Image Arguments',
        'If --use-pathways is provided, user wants to set up a'
        'Pathways workload on xpk.'
    )
)

### "workload create" Required arguments
workload_create_parser_required_arguments.add_argument(
    '--workload',
    type=workload_name_type,
    default=None,
    help='The name of the workload to run.',
    required=True,
)
workload_create_parser_required_arguments.add_argument(
    '--command',
    type=str,
    default=None,
    help=(
        'Main command to run on each VM. This script runs within the docker '
        'container. Typically this looks like "--command=\'python3 train.py\'" '
        'but if your docker container is missing the dependencies, it might '
        'look more like "--command=\'bash setup.sh && python3 train.py\'".'
    ),
    required=True,
)
workload_create_parser_required_arguments.add_argument(
    '--cluster',
    type=str,
    default=None,
    help='The name of the cluster to run the job on.',
    required=True,
)

workload_device_group = workload_create_parser_required_arguments.add_mutually_exclusive_group(required=True)

workload_device_group.add_argument(
    '--tpu-type',
    type=str,
    default=None,
    help='The tpu type to use, v5litepod-16, etc.'
)
workload_device_group.add_argument(
    '--device-type',
    type=str,
    default=None,
    help='The device type to use (can be tpu or gpu or cpu), v5litepod-16, h100-80gb-8, n2-standard-32-4 etc.'
)

### "workload create" Optional Arguments
add_shared_arguments(workload_create_parser_optional_arguments)

workload_create_parser_optional_arguments.add_argument(
    '--docker-name',
    type=str,
    default='jax-tpu',
    help=(
        'The name of the docker-image to use, default and typically `jax-tpu`.'
    ),
)
workload_docker_image_arguments.add_argument(
    '--docker-image',
    type=str,
    help=(
        'The version of the docker-image to use. By default, '
        ' `--base-docker-image` is used. Set this argument if the user wants'
        ' the docker image to be used directly by the xpk workload.'
        ' a custom docker image it is typically addressed as'
        ' gcr.io/${PROJECT}/${NAME}:latest. This docker image will be used'
        ' directly by the xpk workload.'
    ),
)
workload_base_docker_image_arguments.add_argument(
    '--base-docker-image',
    type=str,
    default=default_docker_image,
    help=(
        f'The base docker-image to use, default {default_docker_image}. If'
        ' using a custom docker image it is typically addressed as'
        ' gcr.io/${PROJECT}/${NAME}:latest. This docker image will be used as a'
        ' base image by default and the `--script-dir` by default'
        ' will be added to the image.'
    ),
)
workload_base_docker_image_arguments.add_argument(
    '--script-dir',
     type=directory_path_type,
     default=default_script_dir,
    help='The local location of the directory to copy to the docker image and'
        ' run the main command from. Defaults to current working directory.'
)
workload_create_parser_optional_arguments.add_argument(
    '--num-slices',
    type=str,
    default=1,
    help='The number of slices to use, default=1.',
)
workload_create_parser_optional_arguments.add_argument(
    '--num-nodes',
    type=str,
    default=1,
    help='The number of nodes to use, default=1.',
)
workload_create_parser_optional_arguments.add_argument(
    '--env-file',
    type=str,
    default=None,
    help=(
        'Environment file to be applied to the container.  This file should '
        'use the syntax <variable>=value (which sets the variable to the given '
        'value) or <variable> (which takes the value from the local '
        'environment), and # for comments.'
    ),
)
workload_create_parser_optional_arguments.add_argument(
    '--priority',
    type=str,
    default='medium',
    choices=['very-low', 'low', 'medium', 'high', 'very-high'],
    help=(
        'A priority, one of `very-low`, `low`, `medium`, `high` or `very-high`.'
        ' Defaults to `medium`.'
    ),
)
workload_create_parser_optional_arguments.add_argument(
    '--scheduler',
    type=str,
    default='default-scheduler',
    help=(
        'Which scheduler you want to use. Defaults to `default-scheduler`.'
        'If your cluster is configured for high throughput scheduling you might'
        'want to use `gke.io/high-throughput-scheduler`.'
    ),
)
workload_create_parser_optional_arguments.add_argument(
    '--max-restarts',
    type=str,
    default='0',
    help=(
        'Maximum number of times the JobSet will be restarted upon failure. '
        'Defaults to 0.'
    ),
)
workload_create_parser_optional_arguments.add_argument(
    '--debug-dump-gcs',
    type=str,
    default=None,
    help=(
        'GCS bucket or a directory within a bucket, e.g gs://bucket/subdir, '
        'where debugging information such as HLO dumps are uploaded'
    ),
)
workload_create_parser_optional_arguments.add_argument(
    '--enable-debug-logs',
    action='store_true',
    help=(
        'Set this flag to get verbose logging to investigate the issue in the workload.'
    ),
)
workload_create_parser_optional_arguments.add_argument(
    '--deploy-stacktrace-sidecar',
    action='store_true',
    help=(
        'Add this argument to deploy a sidecar container that will '
        'read the stack traces collected in /tmp/debugging directory '
        'and forward them to Cloud Logging for TPU workloads.'
    ),
)
workload_create_parser_optional_arguments.add_argument(
    '-tgps', '--termination-grace-period-seconds',
    type=str,
    default='30',
    help=(
        'Maximum wait time for a workload Pod to wrap up after a disruption event or deletion request.'
        'Defaults to 30 seconds.'
    ),
)
workload_pathways_workload_arguments.add_argument(
    '--use-pathways',
    action='store_true',
    help=(
        'Provide this argument to create Pathways workloads.'
    ),
)
workload_pathways_workload_arguments.add_argument(
    '--proxy-server-image',
    type=str,
    default='gcr.io/cloud-tpu-v2-images/pathways/pathways-demo:proxy_server',
    help=(
        'Please provide the proxy server image for Pathways.'
    ),
)
workload_pathways_workload_arguments.add_argument(
    '--server-image',
    type=str,
    default='gcr.io/cloud-tpu-v2-images/pathways/pathways-demo:server',
    help=(
        'Please provide the server image for Pathways.'
    ),
)
workload_pathways_workload_arguments.add_argument(
    '--pathways-gcs-location',
    type=str,
    default='gs://cloud-pathways-staging/tmp',
    help=(
        'Please provide the GCS location to store Pathways artifacts.'
    ),
)
workload_create_parser.set_defaults(func=workload_create)

# "workload delete" command parser.
workload_delete_parser = workload_subcommands.add_parser(
    'delete', help='Delete job.'
)
workload_delete_parser_required_arguments = (
    workload_delete_parser.add_argument_group(
        'Required Arguments',
        'Arguments required for `job delete`.',
    )
)
workload_delete_parser_optional_arguments = (
    workload_delete_parser.add_argument_group(
        'Optional Arguments', 'Arguments optional for `job delete`.'
    )
)
add_shared_arguments(workload_delete_parser_optional_arguments)

### "workload delete" Required arguments
workload_delete_parser_required_arguments.add_argument(
    '--cluster',
    type=str,
    default=None,
    help='The name of the cluster to delete the job on.',
    required=True,
)
### "workload delete" Optional arguments
workload_delete_parser_optional_arguments.add_argument(
    '--workload',
    type=workload_name_type,
    default=None,
    help='The name of the workload to delete. If the workload is not specified, '
    'all workloads will be deleted from the cluster.',
)
workload_delete_parser_optional_arguments.add_argument(
    '--filter-by-job',
    type=str,
    help='Filters the arguments based on job name. Provide a regex expression'
          'to parse jobs that match the pattern or provide a job name to delete a single job.',
)
workload_delete_parser_optional_arguments.add_argument(
    '--filter-by-status',
    type=str,
    default='EVERYTHING',
    choices=['EVERYTHING', 'FINISHED', 'RUNNING', 'QUEUED', 'FAILED', 'SUCCESSFUL'],
    help='Filters the arguments based on status. Selected filters are listed'
        ' above. FAILED and SUCCESSFUL are sub-states of FINISHED.',
    required=False,
)
workload_delete_parser_optional_arguments.add_argument(
    '--force',
    action='store_true',
    help=(
      'Forces workload deletion command to run without additional approval.'
    ),
)

workload_delete_parser.set_defaults(func=workload_delete)

# "workload list" command parser.
workload_list_parser = workload_subcommands.add_parser(
    'list', help='List jobs.'
)

workload_list_parser.add_argument(
    '--cluster',
    type=str,
    default=None,
    help='The name of the cluster to list jobs on.',
    required=True,
)

workload_list_parser.add_argument(
    '--filter-by-status',
    type=str,
    default='EVERYTHING',
    choices=['EVERYTHING', 'FINISHED', 'RUNNING', 'QUEUED', 'FAILED', 'SUCCESSFUL'],
    help='Filters the arguments based on status. Selected filters are listed'
        ' above. FAILED and SUCCESSFUL are sub-states of FINISHED.',
    required=False,
)

workload_list_parser.add_argument(
    '--filter-by-job',
    type=str,
    help='Filters the arguments based on job name. Provide a regex expression'
          'to parse jobs that match the pattern or provide a job name to view a single job.',
    required=False,
)
add_shared_arguments(workload_list_parser)

workload_list_parser.set_defaults(func=workload_list)


#### "inspector" command parser. ####
inspector_parser = xpk_subcommands.add_parser(
    'inspector', help='commands around investigating workload, and Kueue failures.'
)

inspector_parser.set_defaults(func=default_subcommand_function)
inspector_subcommands = inspector_parser.add_subparsers(
    title='inspector subcommands',
    dest='xpk_inspector_subcommands',
    help='Investigate workload, and Kueue failures.',
)

inspector_parser_required_arguments = (
    inspector_parser.add_argument_group(
        'inspector Built-in Arguments',
        'Arguments required for `inspector`.'
    )
)
inspector_parser_optional_arguments = (
    inspector_parser.add_argument_group(
        'Optional Arguments', 'Arguments optional for `inspector`.'
    )
)

### "inspector" Required arguments

inspector_parser_required_arguments.add_argument(
    '--cluster',
    type=str,
    default=None,
    help='The name of the cluster to investigate.',
    required=True,
)

### "inspector" Optional Arguments
add_shared_arguments(inspector_parser_optional_arguments)

inspector_parser_optional_arguments.add_argument(
    '--workload',
    type=workload_name_type,
    default=None,
    help='The name of the workload to investigate.',
)

inspector_parser_optional_arguments.add_argument(
    '--print-to-terminal',
    action='store_true',
    help=(
      'Prints inspector output to terminal. A user can always look at the returned file.'
    ),
)

inspector_parser.set_defaults(func=inspector)

xpk_print('Starting xpk', flush=True)
main_args = parser.parse_args()
main_args.func(main_args)


################### Main ###################
def main() -> None:
  xpk_print('XPK Done.', flush=True)


if __name__ == '__main__':
  main()
