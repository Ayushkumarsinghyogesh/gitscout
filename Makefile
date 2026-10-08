.PHONY: install test doctor scout watch backfill docker clean

install:
	python -m pip install -e ".[dev]"

test:
	python -m pytest -q

# Validates the token and every GraphQL query against live GitHub. Run this first.
doctor:
	gitscout doctor --targets cloud-security

# One incremental pass over the cloud-security profile, exporting only new finds.
scout:
	gitscout scout --targets cloud-security --incremental --new-only \
		-o out/new-leads.csv -o out/new-leads.jsonl --only-with-email

# First-time full crawl. Capped so it finishes; re-run to continue (it resumes).
backfill:
	gitscout scout --targets cloud-security --max 2000 -o out/leads.csv

# Run the scheduler in the foreground.
watch:
	gitscout watch --targets cloud-security --every 6h -o out/new-leads.csv

docker:
	docker compose up -d --build

clean:
	rm -rf out *.db *.db-wal *.db-shm .pytest_cache
