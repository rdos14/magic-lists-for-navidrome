# MagicLists — Development & Deployment Workflow

## Stack

| Aspect | Detalle |
|---|---|
| Repo fork | `git@github.com:rdos14/magic-lists-for-navidrome.git` |
| Upstream | `https://github.com/rsynnot/magic-lists-for-navidrome.git` |
| Source local | `/volume1/docker/magiclists/repo/` |
| Runtime data | `/volume1/docker/magiclists/state/` (montado como `/app/data`) |
| Env vars | `/volume1/docker/magiclists/.env` |
| Imagen custom | `magiclists-custom:local` (build local, no registry) |
| Puerto | `4534` → `8000` (container) |
| Container name | `magiclists` |

## Git Remotes

- `origin` → `git@github.com:rdos14/magic-lists-for-navidrome.git` (fork propio)
- `upstream` → `https://github.com/rsynnot/magic-lists-for-navidrome.git` (oficial)

## Development Cycle

### 1. Sincronizar con upstream (traer cambios oficiales)

```bash
cd /volume1/docker/magiclists/repo
/opt/bin/git fetch upstream
/opt/bin/git checkout main
/opt/bin/git merge upstream/main
/opt/bin/git push origin main
```

### 2. Hacer cambios locales

Editar archivos en `/volume1/docker/magiclists/repo/backend/`.

Estructura del repo:
- `backend/` — lógica principal (FastAPI)
- `frontend/` — UI
- `recipes/` — recetas de playlists
- `payloads/` — payloads de AI
- `scripts/` — utilidades

### 3. Build de la imagen custom

```bash
cd /volume1/docker/magiclists/repo
/var/packages/ContainerManager/target/usr/bin/docker compose -f docker-compose.custom.yml build
```

### 4. Deploy (reemplazar contenedor en caliente)

```bash
cd /volume1/docker/magiclists/repo
/var/packages/ContainerManager/target/usr/bin/docker compose -f docker-compose.custom.yml up -d
```

Esto recrea el container `magiclists` con `image: magiclists-custom:local`
sin perder datos (el volumen `../state:/app/data` persiste).

### 5. Verificar

```bash
/var/packages/ContainerManager/target/usr/bin/docker logs magiclists --tail 50
curl http://localhost:4534/api/health
```

### 6. Commit y push a tu fork

```bash
cd /volume1/docker/magiclists/repo
/opt/bin/git add -A
/opt/bin/git commit -m "Descripción del cambio"
/opt/bin/git push origin main
```

## Rollback

Si algo sale mal:

```bash
# Backup de runtime data
cp -a /volume1/docker/magiclists/state /volume1/docker/magiclists/state.rollback-$(date +%F-%H%M%S)

# Volver al commit anterior
cd /volume1/docker/magiclists/repo
/opt/bin/git checkout HEAD~1 -- backend/
/var/packages/ContainerManager/target/usr/bin/docker compose -f docker-compose.custom.yml build
/var/packages/ContainerManager/target/usr/bin/docker compose -f docker-compose.custom.yml up -d
```

Backups pre-cutover: `/volume1/docker/magiclists/backups/pre-cutover-live-20260706-222204`

## Notas importantes

- La imagen se builda localmente como `magiclists-custom:local` — no se publica en ningún registry.
- El container usa `restart: unless-stopped` en `docker-compose.custom.yml`.
- La password de Navidrome está en `/volume1/docker/magiclists/.env`.
- `/opt/bin/git` es la ruta del binario `git` en Synology DSM.
- `/var/packages/ContainerManager/target/usr/bin/docker` es el binario `docker` en Synology DSM.
