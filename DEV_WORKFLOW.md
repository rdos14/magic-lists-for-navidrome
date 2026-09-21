# MagicLists — desarrollo en Windows y deploy en Synology

## Regla principal

El desarrollo se hace en **Visual Studio Code (VS Code) en Windows 11**, sobre un clon local del fork de GitHub.

No se desarrolla editando directamente el código dentro del contenedor ni dentro de `/volume1/docker/magiclists-data`.

Flujo:

```text
Windows 11 + VS Code + clon local
        ↓ git push
GitHub fork: rdos14/magic-lists-for-navidrome
        ↓ git pull (Git ya disponible en Synology)
Synology: /volume1/docker/magiclists/repo
        ↓ Docker build local
Container Manager: proyecto magiclists-dev
        ↓
magiclists-dev_magiclists_data → /app/data
```

## Repositorios

| Elemento | Valor |
|---|---|
| Fork/origin | `git@github.com:rdos14/magic-lists-for-navidrome.git` |
| Upstream | `https://github.com/rsynnot/magic-lists-for-navidrome.git` |
| Rama de trabajo | `main` |
| Repo local Windows | `C:\Tools\Scripts\github-projects\magic-lists-for-navidrome` |
| Repo en Synology | `/volume1/docker/magiclists/repo` |

No incluir nunca `.env`, tokens, contraseñas ni claves en Git.

## Desarrollo en Windows con VS Code

### Primera instalación

En PowerShell:

```powershell
cd C:\Tools\Scripts\github-projects
git clone git@github.com:rdos14/magic-lists-for-navidrome.git
cd magic-lists-for-navidrome
code .
```

Si el clon ya existe:

```powershell
cd C:\Tools\Scripts\github-projects\magic-lists-for-navidrome
git switch main
git pull --ff-only
code .
```

Editar principalmente:

```text
backend/       API FastAPI y lógica de playlists
frontend/      interfaz web
recipes/       recetas y parámetros de curación
payloads/      payloads auxiliares
scripts/       utilidades
```

Antes de commit:

```powershell
git status
git diff
git diff --check
python -m compileall backend
```

Commit y push desde Windows:

```powershell
git add -A
git commit -m "Describe the change"
git push origin main
```

## Estado actual de Synology

| Elemento | Valor actual |
|---|---|
| Compose activo | `/volume1/docker/magiclists/repo/docker-compose.yml` |
| Proyecto Compose | `magiclists-dev` |
| Servicio/contenedor | `magiclists` |
| Imagen local | `magiclists-dev-magiclists` |
| Puerto | `4534:8000` |
| Env file | `/volume1/docker/magiclists/.env` |
| Base dentro del contenedor | `/app/data/magiclists.db` |
| Persistencia activa | `magiclists-dev_magiclists_data` → `/app/data` |
| Docker | `/var/packages/ContainerManager/target/usr/bin/docker` |

El named volume activo contiene la base restaurada de MagicLists. No editar directamente `/volume1/@docker/volumes`.

Estas rutas son históricas y no son el runtime activo:

```text
/volume1/docker/magiclists/state
/volume1/docker/magiclists-data
/volume1/docker/magiclists/repo/docker-compose.custom.yml
```

`state` conserva una copia histórica/rollback. `magiclists-data` es una ruta legacy. No cambiar el Compose activo a esas rutas sin una migración separada y una copia verificada.

## Preflight de Git en Synology

Git está instalado en el NAS mediante el paquete `git`:

```text
/usr/local/bin/git → /volume1/@appstore/git/bin/git
git version 2.55.0
```

El repo local ya está vinculado a `origin` y `upstream`. Verificar siempre el estado antes de sincronizar; no hacer `pull` si hay cambios locales.

Verificar primero:

```bash
for p in /opt/bin/git /usr/bin/git /usr/local/bin/git /var/packages/Git/target/usr/bin/git; do
  [ -x "$p" ] && printf '%s\n' "$p"
done
```

Luego sincronizar solamente con fast-forward:

```bash
GIT=/usr/local/bin/git
[ -x "$GIT" ] || { echo 'GIT_NOT_AVAILABLE'; exit 1; }
"$GIT" -C /volume1/docker/magiclists/repo status --short --branch
"$GIT" -C /volume1/docker/magiclists/repo pull --ff-only
```

Si existen cambios locales, detenerse y respaldarlos antes de hacer pull.

## Build y deploy actual

Después de que el cambio esté en el fork y el repo del NAS esté sincronizado:

```bash
D=/var/packages/ContainerManager/target/usr/bin/docker
F=/volume1/docker/magiclists/repo/docker-compose.yml

"$D" compose -p magiclists-dev -f "$F" config -q
"$D" compose -p magiclists-dev -f "$F" build magiclists
"$D" compose -p magiclists-dev -f "$F" up -d --no-deps --force-recreate magiclists
```

`--force-recreate` recrea únicamente el contenedor de la aplicación; no elimina el named volume porque no se usa `down -v` ni ningún flag de volumen.

No ejecutar durante recuperación:

```text
docker compose down -v
docker system prune
docker volume prune
docker volume rm magiclists-dev_magiclists_data
```

## Verificación obligatoria

```bash
D=/var/packages/ContainerManager/target/usr/bin/docker

"$D" inspect magiclists --format \
'STATUS={{.State.Status}} HEALTH={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} RESTART={{.RestartCount}}'

curl -fsS --max-time 15 http://127.0.0.1:4534/system-check
curl -fsS --max-time 15 http://127.0.0.1:4534/api/playlists
"$D" logs --since 5m --tail 100 magiclists
```

Para una verificación SQLite sin imprimir datos de playlists:

```bash
"$D" exec magiclists python3 -c \
'import sqlite3; c=sqlite3.connect("file:/app/data/magiclists.db?mode=ro",uri=True); print("playlists=",c.execute("select count(*) from playlists").fetchone()[0],"scheduled=",c.execute("select count(*) from scheduled_playlists").fetchone()[0],"integrity=",c.execute("pragma integrity_check").fetchone()[0])'
```

Criterios mínimos:

```text
health=healthy
restart=0
/system-check → HTTP 200
integrity=ok
```

## Rollback

Antes de modificar código o datos, conservar:

- hash del archivo modificado;
- copia del archivo original;
- image ID anterior;
- backup del named volume o de `magiclists.db`.

El rollback de código debe hacerse restaurando el archivo/commit respaldado, reconstruyendo la imagen y recreando solo `magiclists`. Nunca borrar el named volume como rollback.

Backups relevantes actuales:

```text
/volume2/temp/magiclists-volume-restore-20260920-221841
/volume2/temp/magiclists-code-backup-20260920-224248
/volume2/temp/magiclists-doc-backup-20260920-225626
```

## Nota sobre el fix actual

El fix de `candidate_tracks` fue aplicado en el repo del NAS y validado con build y una creación real de playlist. Antes de sincronizar desde Windows, ese cambio debe incorporarse al clon local y publicarse en el fork; de lo contrario, un `git pull` posterior puede dejar el repo remoto sin esa corrección.
