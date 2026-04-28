# demo-reset — tiny cron container that hits the backend's demo-reset
# endpoint daily at 00:00 UTC. Self-gates on DEMO_MODE; safe to leave
# running on a clone-and-own deployment (the endpoint will no-op).

FROM alpine:3.19

# busybox cron + curl (for triggering the endpoint)
RUN apk add --no-cache curl

# Copy the cron script + crontab. The script is the actual reset
# trigger; the crontab schedules it.
COPY reset.sh /usr/local/bin/demo-reset.sh
COPY crontab /etc/crontabs/root
RUN chmod +x /usr/local/bin/demo-reset.sh

# Run cron in foreground so the container stays alive. ``-f`` is
# foreground; ``-l 8`` raises the log level so reset runs are visible
# in ``docker-compose logs``.
CMD ["crond", "-f", "-l", "8"]
