# Cove: A Framework for Compositional Multi-Party Confidential Workflows

## What is Cove?

TODO: An introductory paragraph to motivate.

## Getting Started

As Cove is a multi-party framework, how you use Cove depends on which party you are:

* For development:
  * **Security Researcher**: You are interested in running Cove end-to-end to understand
    how it works as well as its inner workings. In this case, you act as all parties
    simultaneously.
* For production:
  * **Workflow Publisher**: You design a Cove workflow, specifying the business logic and
    the required inputs from other parties. It is your responsibility to design the
    workflow by discussing with artifact provisioners, author and publish the workflow,
    and solicit approval from artifact provisioners from.
    * Once workflows are published, anyone can execute them, but for simplicity, we will
      assume that the workflow publisher will also execute the workflows they publish.
  * **Artifact Provisioner**: You are a party who provides private inputs to workflows,
    and may also expect the workflows to generate private outputs that only you can decrypt.
    For example, these private inputs may be proprietary model weights or serving code.
    You expect your private inputs to never be leaked to anyone else. Your responsibility is
    to run the Cove provisioning server (so you can keep secrets local), and to review and
    approve workflows.
  * **End User**: You use a service (e.g., confidential inference) running inside a secure
    enclave as part of a Cove workflow. You wish to ensure that the service is indeed running
    inside an enclave at all times and that it satisfies the claims that the workflow has
    made (e.g., the model passes a certain benchmark; the code will not log your requests).
  * **Registry Provider**: You run a self-hosted CoveHub where workflows, encrypted
    artifacts, certificates, etc. are stored and distributed. This is similar to a Docker
    Registry that anyone can run, except that it is completely untrusted and acts only as a
    data availability layer. We provide covehub.io as a public demo of this service.

### Getting Started as a Workflow Publisher

You will need:
* **A Phala Cloud account**. This is required to run Cove workflows, as the implementation
  currently only works on Phala Cloud. It's worth noting that Cove itself is not designed
  *around* Phala Cloud, and it can be adapted to work with any cloud provider that
  provides a way to run an arbitrary Docker Compose in a TEE VM. To be clear, Phala Cloud is
  not a trusted entity, as Cove still verifies that the workflow executes as part of a valid
  secure enclave.
  * To prototype the workflow, you can use inexpensive CPU TEE instances. Cove will launch
    this type of instances when your workflow does not require GPUs.
* **A domain that you own**. In CoveHub, usernames are domains. User authentication is HTTPS
  certificate validation. Again, CoveHub is untrusted so the authentication is only a DoS
  protection mechanism ensuring that users cannot arbitrarily mutate others' encrypted data.
  You may use any domain that has a valid certificate, including subdomains. It's recommended
  to use Cloudflare as a very simple and inexpensive solution.
* **A Docker Registry**, as you will most likely need to publish custom Docker images. Only
  public images are needed, so a free DockerHub account is sufficient.

You will also need a secure communication channel with artifact provisioners, as you will be
working closely with them to design, publish, and run the workflow.

#### Design the Workflow
Your job as a workflow publisher is to design the workflow. Look at demos/ for starting
examples. At a high level, the workflow includes:
* A list of **artifact provisioners**, each identified by their domain name
* A list of **private artifacts**, each belonging to an artifact provisioner (the artifact owner)
  * Each private artifact can be static or dynamic; static means that the owner already has the
    artifact as a known file, whereas dynamic means it will be the output of some intermediate node.
  * Static artifacts require an artifact hash, which you'll need to obtain from the artifact's
    provisioner.
* A list of **nodes**, each representing a secure enclave. Each node includes
  * An optional list of dependency nodes that must complete before this node can start;
  * A Docker compose file that contains a list of **services** (containers) that shall be run in the enclave;
  * Optional additional specification for each service:
    * **Private input artifacts**: A list of private artifacts that must be provisioned by their
      respective owners into the enclave, as required inputs to the service. Each such input will be
      decrypted as part of the provisioning process, and the decrypted file will be provided as a
      mount at `/workspace/input/<artifact_file_name>` in the service's container.
    * **Preconditions**: An expression for what must hold true between input artifacts and the
      dependency nodes' execution certificates. This is the key logic that verifies the properties of the
      private inputs.
    * **Private output artifacts**: A list of dynamic artifacts that this container is expected to
      generate. By the time the container exits, Cove expects the unencrypted output artifact to be
      made available at `/workspace/output/<artifact_file_name>`. Cove will handle key generation with
      the artifact owner, encrypt the output, and upload it to CoveHub, making it available for
      subsequent nodes or other workflows.
    * **Custom Certificate Fields**: A declaration of custom fields emitted by the container that
      should be included in the node's execution certificate. These fields must carry a JSON schema.
      These fields give semantics to the execution result of the container, allowing subsequent nodes
      or workflows to use them in their preconditions.
    * **Ephemeral Keys**: A request to Cove to generate and sign keypairs, which are then given to the
      container so the service can use them as HTTPS certificates to prove that the service is running
      inside the enclave. These keys are provided as
      `/cove/ephemeral_keypairs/<key_name>/{private,certificate}.pem`.
  * Note that a service may terminate, or it may be long-running. Nodes with long-running services are
    not expected to output an execution certificate, and should not be used as a dependency.

You will also need to design the container Docker images used in the workflow, build them, and push them
to a publicly accessible Docker registry.

#### Publish the Workflow

When you're satisfied with the workflow, run `cove check` and then `cove compile` TODO: uh, what are the
commands again lol... and then publish.

Once published, go to the covehub UI to check your workflow. There will be a graph that depicts the logical
data flow.

#### Solicit Approvals

The next step is to ask artifact provisioners involved in the workflow to approve your workflow. The
process of approving the workflow is described in the "Getting Started as an Artifact Provisioner"
section. We will assume that the workflow has been approved by all artifact provisioners.

#### Run the Workflow

Run the workflow:

```
cove deploy <publisher-domain>/<workflow_id> --phala-instance-type <type>
```

### Getting Started as an Artifact Provisioner

As an artifact provisioner, you need
* A **server** with Internet access.
* A **domain** with HTTPS certificates, pointing to the server. We recommend using **Cloudflare Tunnel** as a very easy way to
  set this up.

We will assume that you have a domain such as `provisioner.alice.com` pointing to your server's `localhost:12345`.

#### Bring up the Provisioning Server

The **provisioning server** is a long running service you control, responsible for the following critical
tasks:
* Generate and store encryption keys for the artifacts you own
* Register and lookup approved workflows (so that keys are only released to approved workflows)
* Securely communicate with running enclaves via mTLS and remote attestation, verifying its
  hardware authenticity and actual running node definition, and if valid and approved, sends
  the requested encryption key to the enclave.

To bring up the provisioning server, simply run `cove provision serve 12345`.

#### Prepare Static Artifacts

Coordinate with the workflow publisher to clarify the requirements for your static artifacts.
For example, you may agree that the format of a "model weights" static artifact be a gzip
file that extracts to exactly five `.safetensor` files. Or you may agree that the format of
your "model serving code patch" artifact be a git diff on top of a specific vLLM commit.

You will then build your static artifacts as agreed upon, register them with your artifact
provisioner, and for each artifact, give the CoveHub path and artifact hash (printed by 
the command) to the workflow publisher.

```
cove provision [--overwrite] <artifact_name> <file_path>
```

This command generates a new encryption key, encrypts your artifact, uploads the encrypted
data to CoveHub, and registers the encryption key with your local key provisioner.

#### Inspect and Approve the Workflow

Once the Workflow Publisher has designed and published the workflow on CoveHub, and asked you to
approve the workflow, first inspect the workflow:

```
cove provision inspect <publisher-domain>/<workflow_id>
```

This prints the workflow definition, allowing you to carefully review it. **It's critically
important that you understand what the workflow is doing, especially all its containers and
preconditions**. Cove cannot protect you from skipping this step or blindly approving workflows.
You must ask yourself:
* What is this workflow trying to accomplish? Is this exactly what I wish to do together with
  the other parties? For example, if I wish to serve a private model, does the workflow do that,
  or does it have an inconspicuous service in there that exports the model weights somewhere
  else?
* What does each container do? Have I inspected the container image at that specific hash, and
  verified that the container's source code and environment are benign? If the container
  contains any binaries on top of an untrusted base, can I verify that the binary is built
  from a source code and that I can reproduce the build? Auditing of the container images is
  beyond the scope of Cove. If you approve the container, Cove considers it approved for your
  purposes.
* What does each precondition do? Just because the workflow looks innocent doesn't mean that
  the behavior is correct. For example, if you wish to supply a private model, and another
  party is supplying a private model serving code, you want to make sure that their code has
  been sufficiently audited; so the precondition should contain something along these lines:
  a dependency audit node emitted a certificate with a successful audit result, whose code
  under audit matches the current node's serving code input. Without this precondition, the
  other party can supply any code, such as one that exfiltrates your model weights.

Once you're confident that the workflow is correct, approve the workflow:

```
cove provision approve <publisher-domain>/<workflow_id>
```

This command does the following: For each node (enclave) in the workflow, approve the concrete
generated docker compose (what is actually being run in the enclave) for each private artifact
owned by you in this node.

### Getting Started as an End User

As an end user, you interact with a service running in an enclave. Let's use a basic confidential 
inference example:
* You wish to verify that the model used to serve the endpoint is one that passes certain
  benchmarks (i.e. it is a capable model and they are not fooling you with a tiny one);
* You wish to verify that your requests and responses are not logged.

Your personal audit of the service endpoint involves two parts:
1. Verifying that the service endpoint is indeed running the exact workflow expected, in a secure
  enclave;
2. Auditing that the preconditions and their dependency execution certificates satisfy your
  semantic expectations.

For the first part, the Cove CLI provides a client proxy. It serves the following purposes:
* Transforms the remote HTTPS/TLS endpoint into a local HTTP/TCP endpoint for easy calling;
* Verfies that the endpoint is
  * Served with a HTTPS/TLS certificate generated and signed with the enclave, where
  * the enclave is a genuine TEE that runs a trusted VM image parametered with the exact 
    Docker Compose expected from the workflow; and that
  * The workflow is signed by the workflow publisher.

```
cove client proxy \
  --remote <service-url>
  --local localhost:8080
  --workflow <publisher-domain>/<workflow_id>
  --write-workflow-to workflows/
```

After verification, this command will also write the workflow to the output directory as
specified. You should then inspect the workflow to make sure you understand it. Refer to
the Inspect and Approve the Workflow section above. In this example, make sure that the
model and code audits as well as the preconditions for the serving step correspond to
what you expect: that the audits use reasonable models and prompts, and that the
preconditions correctly connects the audit results to what is being served.

Once you understand and are satisfied about the endpoint's security, you may use the
local proxied endpoint to call the service and be confident that this goes directly to
the trusted enclave.

### Getting Started as a Registry Provider

As a registry provider, your task is to run CoveHub. A very simple CoveHub server has
been included in the compose.yaml at the root of the repository. All you need is to
provide a Cloudflare Tunnel token to run it behind your domain. You can run the CoveHub
server on any domain and on any server; just make sure it has enough space since
artifacts can be quite large.

