# NEXORA IA v0.4 — fundação para GitHub, Render e Supabase

Este projeto evolui a v0.3 para um **ambiente de desenvolvimento/testes multiusuário**.

## Estado real das funcionalidades

- ✅ OAuth 2.0 + PKCE, login via Deriv e cookies HttpOnly; App ID público parametrizado.
- ✅ Tabela de usuários vinculados a conta **explicitamente identificada como demo** (falha fechado caso estrutura não seja reconhecida).
- ✅ Configurações individuais persistidas em PostgreSQL (Supabase) ou SQLite (apenas desenvolvimento local).
- ✅ Histórico de **operações virtuais** persistido e segregado por usuário.
- ✅ Leitura pública de cotações e pesquisa read-only de contratos e propostas via Deriv.
- ✅ Botão ligar/desligar com motor de simulação isolado por conta; robôs voltam **desligados** após reinício.
- ✅ Sem endpoints `buy`, `sell`, `proposal_open_contract` nem acesso a dinheiro real.
- ⚠️ OAuth e integração de dados ainda não foram testados com uma conta Deriv real neste ambiente.
- ⚠️ O simulador NÃO usa payout oficial: continua com pagamento **hipotético de 80%**, e não simula spread/latência/execução. Não representa resultados em conta demo Deriv.
- ⚠️ O login persiste, mas quando o token Deriv expira é preciso reconectar. Não há refresh token na OAuth Deriv.
- ⚠️ Em `Render Free`, processos hibernam e reiniciam. Este protótipo NÃO é infraestrutura adequada para negociar dinheiro real ou controlar ordens reais.

## Execução local

Python 3.11+:

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

**Configure as variáveis de ambiente** copiando `.env.example` para `.env` e carregando-as no shell/ambiente de desenvolvimento (a aplicação não carrega `.env` automaticamente). Gere a chave:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Coloque o resultado em `NEXORA_FERNET_KEY` como variável secreta. Não envie nem publique essa chave.

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Abra `http://localhost:8000` no mesmo computador que roda o servidor. O callback cadastrado na Deriv é `http://localhost:8000/auth/deriv/callback`.

## Publicar na Render com Supabase (quando estiver preparado)

1. Crie projeto **privado** GitHub e envie o conteúdo desta pasta, nunca `.env` nem credenciais.
2. Crie Supabase Postgres e obtenha a URI de conexão PostgreSQL **do servidor**, mantendo credenciais secretas. Para Render, use o endpoint compatível com a conectividade disponível (pooler/session, conforme o painel Supabase). O código usa driver `psycopg`.
3. Use o `render.yaml` (Blueprint). Configure `DATABASE_URL`, `NEXORA_FERNET_KEY`, `NEXORA_PUBLIC_URL=https://SEU-SERVICO.onrender.com` e `DERIV_REDIRECT_URI=https://SEU-SERVICO.onrender.com/auth/deriv/callback` no painel Render.
4. **Antes de testar o OAuth publicado**, adicione exatamente essa URL HTTPS de callback às Redirect URLs do app registrado na Deriv. Não é suficiente manter o antigo `localhost`.
5. Instância única (`--workers 1`) e modo de teste. Migrações de esquema ainda não estão implementadas; `create_all` apenas cria tabelas ausentes.
6. Banco Supabase deve ter projeto ativo: limites e políticas de suspensão do plano grátis podem mudar.

### Importante sobre os dados e segurança

- Tokens OAuth são criptografados com Fernet antes de persistidos; chaves de criptografia ficam somente em variável secreta do servidor. Não rotacione a chave sem plano de migração.
- Cookies são `Secure` em `NEXORA_PUBLIC_URL` HTTPS e HttpOnly. POSTs feitos pelo navegador são validados quanto à origem quando houver cabeçalho `Origin`; endurecer proteção CSRF e autenticação antes de produção.
- A aplicação identifica usuários pela conta demo retornada. É preciso validar o esquema exato das respostas da Deriv em uma integração efetiva; **não presumir que contas de mesma pessoa têm o mesmo ID**.
- A simulação opera exclusivamente sobre preços públicos de **um símbolo**, não é um AutoPilot multiativo validado. A estratégia de médias móveis é experimental e sem vantagem comprovada.
- Sem engine durável de tarefas: um restart interrompe/cancela operações virtuais abertas; histórico só registra operações virtuais concluídas. É um protótipo, não um executor resiliente de contratos.
- `GET /healthz` é apenas health-check; não ping para garantir operação contínua. Um plano gratuito pode dormir ou reiniciar independentemente do monitoramento.
- **Não habilite modo real**, tráfego comercial ou captação de clientes brasileiros da Deriv sem avaliação de compatibilidade com as regras da corretora e restrições da CVM.

## Testes

```bash
python -m pytest -q
```

## Próximas etapas para o Manus

1. Validar OAuth no Render e formato da API atual, com mocks e conta de teste autorizada.
2. Implementar migrações Alembic, sessão anti-CSRF robusta, logging/auditoria e rate limits.
3. Implementar executor **exclusivamente demo**, segregado por conta: obter URL OTP demo, `proposal`, `buy`, `proposal_open_contract`, reconciliação pós-restart, limites por conta, idempotência, monitoramento e alarmes. **Jamais usar endpoint real**.
4. Substituir resultados hipotéticos por preços e liquidações oficiais, testar em conta demo e fazer backtest correto (dados fora da amostra, custos, payout, horários).
5. Só então avaliar versão comercial e assinaturas com análise regulatória.

## Links
- OAuth/PKCE: https://developers.deriv.com/docs/intro/oauth/
- API REST e WebSocket: https://developers.deriv.com/docs/intro/api-overview/
- Render grátis: https://render.com/docs/free


## Endereços confirmados para implantação com Manus

Consulte `INFRAESTRUTURA_MANUS.md` e `PROJECT_ENDPOINTS.json` para o GitHub, Supabase, Render e Deriv já criados pelo responsável. Não confunda URL do dashboard Render com URL pública da aplicação.
