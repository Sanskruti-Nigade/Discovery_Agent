# Discovery Agent UI (`ui/`)

React (Vite) front end, ServiceNow colour scheme. Pages:

| URL | Page | Backend |
|---|---|---|
| `/` | **Ask** (landing): lineage Q&A chat | `/api/ask`, `/api/health` (graph dev) |
| `/graph` | Lineage graph explorer | `/api/graph/*`, `/lib/vis-network.min.js` (graph dev) |
| `/reviews` | **Mapping reviews**: *Needs approval* queue (approve selected / reject) and *All reviews* list | `/api/schema-mapping/*` (`mapping/`) |

## Run

```bash
cd ui
npm install
npm run build              # writes ui/dist; frontend/app.py then serves it on http://localhost:8080
```

Development with hot reload: start the backend (`cd frontend && python -m uvicorn app:app --port 8080`),
then `npm run dev` here and open http://localhost:5173 (`/api` and `/lib` are proxied to 8080).

Without a build, `frontend/app.py` keeps serving the original HTML chat and graph pages.

## Branding

- Colours are CSS variables at the top of `src/styles.css` (`--sn-infinite-blue` #032D42 header,
  `--sn-wasabi` #62D84E accent, `--sn-jelly-bean` #276C97 links).
- **Logo:** put the official ServiceNow logo (reversed / white version, for the dark header) at
  `ui/public/servicenow-logo.svg`, then rebuild. Until the file exists the top bar shows the text "ServiceNow".
  Use the asset from the client's brand team; it is not included in the repo.
- Optional `ui/.env` (see `.env.example`): `VITE_APP_NAME` (default "Discovery Agent"), `VITE_LOGO_URL`,
  `VITE_MAPPING_API_BASE` (only when the schema-mapping API runs as its own service, e.g. `http://localhost:8001`).
