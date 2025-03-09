Integrations
------------

This guide explains how to integrate Fujin with CI/CD platforms for automated deployments.

CI/CD Integration Setup
=======================

To integrate Fujin with CI/CD platforms like GitLab CI or GitHub Actions, follow these steps:

1. SSH Key Setup
****************

You have two options for SSH authentication:

**Option 1: Use your existing SSH key (Recommended)**
   - If you're already using Fujin with this host on your laptop, you already have a working SSH key
   - Copy your private key to use in your CI/CD environment

**Option 2: Generate a new deployment key**
   - Generate a new SSH key pair specifically for CI/CD:
     
     .. code-block:: bash
         
         ssh-keygen -t ed25519 -C "deployment@example.com" -f deployment_key
   
   - Add the public key to your server's authorized keys:
     
     .. code-block:: bash
         
         ssh-copy-id -i deployment_key.pub user@your-server.com

2. Configure CI/CD Environment Variables
****************************************

Add your private SSH key as a CI/CD environment variable:

- In GitLab: Go to Settings → CI/CD → Variables
- In GitHub: Go to Settings → Secrets and variables → Actions

Create a variable named ``SSH_PRIVATE_KEY`` with the contents of your private key.

3. Configure Host Environment Variables
***************************************

Instead of using an environment file, use the ``env`` property in your host configuration:

.. code-block:: toml
    :caption: fujin.toml

    [host]
    domain_name = "example.com"
    user = "deploy"
    # Use this instead of envfile for CI/CD environments
    env = """
    DEBUG=False
    SECRET_KEY=$SECRET_KEY
    DATABASE_URL=$DATABASE_URL
    """

For sensitive values, use the secrets feature to fetch from environment variables:

.. code-block:: toml
    :caption: fujin.toml

    [secrets]
    adapter = "system"  # Use system environment variables

The secret values will be substituted from the CI/CD environment variables. See the `secrets documentation </secrets.html>`_ for other secret manager options like Bitwarden, 1Password, or Doppler.

Gitlab CI / CD
==============

Here's a complete example for GitLab CI/CD:

.. code-block:: yaml
    :caption: gitlab-ci.yml

    stages:
      - deploy

    deploy:
      stage: deploy
      image: alpine:latest
      before_script:
        - mkdir -p ~/.ssh
        - echo "$SSH_PRIVATE_KEY" | tr -d '\r' > ~/.ssh/id_rsa
        - chmod 600 ~/.ssh/id_rsa
        - ssh-keyscan -H example.com >> ~/.ssh/known_hosts
      script:
        - curl -LsSf https://astral.sh/uv/install.sh | sh
        - $HOME/.local/bin/uv tool install --upgrade fujin-cli
        - $HOME/.local/bin/fujin --version
        - $HOME/.local/bin/fujin deploy
      tags:
        - production
      only:
        - main

Make sure to:
1. Set ``SSH_PRIVATE_KEY`` in your GitLab CI/CD variables
2. Set any secret environment variables needed by your application
3. Replace ``example.com`` with your actual server domain in the ssh-keyscan command

Github Actions
==============

Here's a complete example for GitHub Actions:

.. code-block:: yaml
    :caption: .github/workflows/deploy.yml

    name: Deploy Application
    
    on:
      push:
        branches: [ main ]
    
    jobs:
      deploy:
        runs-on: ubuntu-latest
        
        steps:
        - uses: actions/checkout@v3
        
        - name: Set up SSH
          run: |
            mkdir -p ~/.ssh
            echo "${{ secrets.SSH_PRIVATE_KEY }}" > ~/.ssh/id_rsa
            chmod 600 ~/.ssh/id_rsa
            ssh-keyscan -H example.com >> ~/.ssh/known_hosts

        - name: Install uv
          uses: astral-sh/setup-uv@v5
        
        - name: Install Fujin
          run: uv tool install --upgrade fujin-cli
        
        - name: Deploy with Fujin
          run: fujin deploy
          env:
            # Add your application's secret environment variables here
            SECRET_KEY: ${{ secrets.SECRET_KEY }}
            DATABASE_PASSWORD: ${{ secrets.DATABASE_PASSWORD }}

Make sure to:
1. Create the ``SSH_PRIVATE_KEY`` secret in your GitHub repository
2. Create any application secret variables in your GitHub repository
3. Replace ``example.com`` with your actual server domain in the ssh-keyscan command