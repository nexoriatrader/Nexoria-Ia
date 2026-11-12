# Restauração rápida

Extraia o ZIP e use o conteúdo de `source/` como raiz do projeto. Instale Python 3.12 e as dependências de `requirements.txt`. Não crie ou publique segredos dentro do repositório.

Para desenvolvimento local, configure SQLite, uma chave Fernet nova, o callback local e execute `pytest -q`. Depois inicie com `uvicorn app.main:app --host 127.0.0.1 --port 8000`.

Para Render, configure `DATABASE_URL` do Supabase, `NEXORA_FERNET_KEY`, `NEXORA_PUBLIC_URL`, `DERIV_REDIRECT_URI`, `DERIV_CLIENT_ID` e `NEXORA_SYMBOL` no painel de segredos. Use o comando presente em `render.yaml` com um worker. No portal Deriv, cadastre exatamente o callback HTTPS público.

O arquivo `metadata/nexora-ia-history.bundle` pode restaurar o histórico com `git clone metadata/nexora-ia-history.bundle nexora-ia-restored`.
