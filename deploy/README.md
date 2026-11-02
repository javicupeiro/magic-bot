# Render Free demo

This deployment serves FastAPI and the existing web UI from one Docker service.
`render.yaml` selects the Free plan explicitly; no paid disks or databases are used.
The model provider still charges for requests.

## Rules bundle

`rules.jsonl.gz` contains the 4,020 indexed rules/glossary/section records and their
existing embeddings. It contains no conversation history, credentials, embedding
cache, or native HNSW files. The source is the rulebook already in this repository.
`Dockerfile.render` rebuilds the native Chroma index during the image build with
the locked dependency version. This requires no API key and works across CPU/OS
architectures. Normal startup does not ingest or regenerate embeddings.

After deliberately updating the local index, regenerate and commit the bundle:

```sh
uv run --locked python deploy/index_bundle.py export
```

## Deploy

Create a Render Blueprint from this repository using `render.yaml`, and enter
`OPENAI_API_KEY` in Render's secret environment field. Alternatively create a Web
Service with Docker, select the Free instance and Frankfurt region, set Dockerfile
Path to `./Dockerfile.render`, health check to `/health`, and add the environment
variables declared in `render.yaml`. Leave Docker Command empty.

Open `https://<service>.onrender.com/ui/`. Test a rules question with sources, a
card search and a follow-up. The root URL is API help, not the chat page.

## Local verification

```sh
docker build -f Dockerfile.render -t magic-bot-render:free .
# Set OPENAI_API_KEY in your shell first; Docker forwards it without printing it.
docker run --rm --name magic-bot-render-check --memory=512m --cpus=0.1 \
  -e OPENAI_API_KEY -p 127.0.0.1:8016:8000 magic-bot-render:free
```

The image supports Render's `PORT`, defaults to 8000, uses one Uvicorn process
and executes one chat turn at a time (`MAGIC_BOT_MAX_WORKERS=1`). No host data
directory is mounted. Do not pass secrets as build arguments.

## Demo limits

- Free services sleep after 15 minutes of inactivity; waking can take about a minute.
- Conversation history is temporary and shared by demo visitors. Do not enter
  personal data. Restarts, redeployments and sleep can erase it.
- The rules index survives as part of the image and is available again on startup.
- This is the existing demo without authentication, user isolation or distributed
  execution. Limit the distribution of the URL and monitor model API usage.
- The rules bundle supports the default vector retrieval. Hybrid retrieval also
  requires the matching parsed JSONL, which this demo image does not include.

References: [Render Free](https://render.com/docs/free),
[Docker](https://render.com/docs/docker),
[Blueprint specification](https://render.com/docs/blueprint-spec).
