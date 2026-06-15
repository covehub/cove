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
    the required inputs from other parties. It is your responsibility to publish the
    workflow, solicit action from the other involved parties, and submit the workflow for
    execution.
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

#### Design the Workflow
Your job as a workflow publisher is to design the workflow. Look at demos/ for starting
examples. At a high level, the workflow includes:
* A list of **artifact provisioners**, each identified by their domain name
* A list of **private artifacts**, each belonging to an artifact provisioner (the artifact owner)
  * Each private artifact can be static or dynamic; static means that the owner already has the
    artifact as a known file, whereas dynamic means it will be the output of some intermediate node.
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

Run the workflow with TODO: how to run?

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

#### Prepare Static Artifacts

To allow your provisioner to send private artifacts into actual enclaves, for each static
input artifact you own, use the following command to generate an encryption key, encrypt
the artifact, upload the encrypted artifact to CoveHub, and register the key and artifact
with your provisioner:

```
cove provision [--overwrite] <artifact_name> <file_path>
```






