---
name: safe-commit
description: Commitea y pushea cambios a GitHub de forma segura, ordenada y descriptiva. Verifica secretos, separa cambios lógicos en commits distintos, escribe mensajes claros que expliquen el "por qué", y solo pushea cuando todo está limpio. Usalo cuando cierres una unidad coherente de trabajo (fix completo, feature terminada, refactor cerrado) — no en mitad de una tarea.
---

# safe-commit

Skill para commit + push a GitHub en `reporte_app`. Aplicalo cuando termines una unidad coherente de trabajo. NO lo uses en mitad de una tarea o con código a medio terminar.

## Cuándo invocarlo

Sí:
- Fix completo verificado contra datos reales.
- Feature terminada (formulario, endpoint, query nueva).
- Refactor cerrado que deja el código en estado coherente.
- Bump de dependencia que ya quedó probado.

No:
- Snapshot intermedio "por si acaso".
- Código que todavía no probaste.
- Cambios mezclados que tocan cosas no relacionadas (en ese caso separá antes).

## Procedimiento

### 1. Diagnóstico

Corré en paralelo:
```bash
git status               # qué se va a commitear (sin -uall para no romper en repos grandes)
git diff                 # cambios staged + unstaged
git log --oneline -5     # estilo de los commits previos para imitarlo
```

### 2. Verificación de seguridad

Antes de stagear:
- `git check-ignore .env debug.log` — confirmar que archivos sensibles están en `.gitignore`.
- Revisar el diff buscando: credentials, tokens, API keys, URLs internas con secretos, dumps de DB.
- Si un archivo sensible apareció modificado, **NO lo commitees** y avisá al usuario.
- Nunca uses `git add .` o `git add -A` — siempre por nombre de archivo.

### 3. Agrupación lógica

Si los cambios son lógicamente distintos, hacé **commits separados**. Ejemplo de esta sesión:
- `main.py` (matching case-insensitive) → commit 1
- `calculations.py` + `connectors/instance.py` (fórmula MTD) → commit 2

Cada commit debe ser comprensible y reversible por sí mismo.

### 4. Mensaje de commit

Formato (sin emojis, sin Co-Authored-By a menos que el usuario lo pida):

```
<Verbo imperativo + resumen en <70 chars>

<Por qué este cambio existe — el contexto que no se ve en el diff.
 Mencioná el problema concreto que resolvía, datos reales si aplica,
 o la decisión de diseño tomada.>

<Si hay >1 archivo o >1 cambio, lista breve de qué hace cada parte.>

<Si verificaste contra datos / casos / clientes específicos, listalos.>
```

Estilo en este repo (mirá `git log` antes de redactar):
- Imperativo: "Add", "Fix", "Read", "Match", "Replace" — no "Added", "Fixing".
- Subject ≤70 chars, sin punto final.
- Body explica el porqué, no el qué (el diff ya muestra qué).
- Sin emojis.

### 5. Commit con HEREDOC

Usá heredoc para preservar formato multilínea:
```bash
git commit -m "$(cat <<'EOF'
Subject line

Body paragraph explaining why.

- Bullet point if needed.
EOF
)"
```

### 6. Verificar y pushear

```bash
git status                  # working tree clean
git log --oneline -3        # confirmar los commits
git push origin main        # push solo si todo OK
```

Si el branch tiene tracking remoto y está al día, `git push` solo. Si es branch nuevo, `git push -u origin <branch>`.

## Reglas duras

- **Nunca** `git push --force` o `--force-with-lease` a main sin permiso explícito.
- **Nunca** `--no-verify` ni `--no-gpg-sign`.
- **Nunca** `git rebase -i` (interactivo, no funciona acá).
- **Nunca** `git commit --amend` sobre commits ya pusheados.
- **Nunca** stagear archivos sin nombrarlos (`add .` / `add -A`).
- Si un hook pre-commit falla, arreglá la causa y hacé **commit nuevo**, no `--amend`.

## Ejemplo de uso correcto (esta sesión)

```bash
git status
# M calculations.py
# M connectors/instance.py
# M main.py

git diff main.py                                 # case-insensitive match
git diff calculations.py connectors/instance.py  # fórmula MTD

git check-ignore .env debug.log
# .env
# debug.log  (ambos ignorados, OK)

git add main.py
git commit -m "Match cliente case-insensitive against MCP results
...
"

git add calculations.py connectors/instance.py
git commit -m "Read venta MTD from products_in_orders for accurate line-item totals
...
"

git push origin main
```
