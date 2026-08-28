# Production monitoring

This stack uses native CloudWatch so the two-vCPU production instance remains
available to the API and render workers. It adds host metrics, retained logs, a
single dashboard, and SNS alerts for instance health, sustained CPU, memory,
disk usage, and application errors.

## One-time setup

1. Attach the AWS managed `CloudWatchAgentServerPolicy` to the EC2 instance
   profile. Keep the existing SSM and Secrets Manager permissions.
2. Deploy `stack.yml` in `us-east-1`, passing the production instance id and
   alert email. Confirm the SNS subscription from that mailbox.
3. On the instance, at the deployed backend commit, run:

   ```bash
   sudo deploy/aws/monitoring/install-cloudwatch-agent.sh
   ```

4. Wait two to five minutes, then verify `MerakiAI-Production` has data for
   memory and root disk. Send a test SNS message before relying on alerts.

The log groups retain access/render logs for 14 days, application logs for 30
days, and deployment logs for 90 days. Adjust retention in `stack.yml`, not in
the console, so later deployments do not undo the policy.
