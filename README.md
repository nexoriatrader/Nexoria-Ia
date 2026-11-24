# NEXORA IA — organização do projeto

Este repositório contém a aplicação web Nexora IA. O código principal declara a versão 0.8.0; a documentação de v0.5.2 abaixo é um registro histórico, não uma descrição verificada da versão publicada.

## Referências de manutenção

- Interface principal: `app/index.html`; administração: `app/admin.html`.
- Backend: `app/main.py`; persistência: `app/db.py`.
- Segurança: [docs/GITHUB_SECURITY.md](docs/GITHUB_SECURITY.md) e [docs/RELEASE_SECURITY_RLS.md](docs/RELEASE_SECURITY_RLS.md).
- Restauração: [docs/RESTORE.md](docs/RESTORE.md).
- Dependências: `requirements.txt`; infraestrutura: `render.yaml`.

Os documentos antigos de instruções aos agentes foram retirados para reduzir ambiguidades. Os registros históricos de versões, os testes existentes e os documentos de segurança foram preservados. Não interpretar documentação histórica como autorização para modificar configurações, realizar operações financeiras ou publicar alterações. Qualquer mudança exige revisão e validação isolada.

---

## Histórico: NEXORA IA v0.5.2 — validação supervisionada Deriv Demo

NEXORA IA é um protótipo de teste com dois fluxos separados: simulação local baseada em cotações públicas e contratos supervisionados exclusivamente na conta Deriv Demo. Não há compra, rota WebSocket ou credencial de conta real no executor.

## Estado do pacote

- OAuth 2.0 + PKCE; a aplicação só abre sessão quando identifica uma conta demonstrativa/virtual.
- Saldo/moeda oficiais podem ser consultados no canal autenticado demo. Propostas oficiais são read-only.
- O botão `TESTAR COMPRA DEMO` pede confirmação na interface. O backend exige a flag do servidor, modo demo e reserva persistente/idempotente antes da chamada remota.
- Há no máximo um contrato pendente/ativo por usuário. Stop Win/Stop Loss bloqueiam novas entradas de acordo com o P/L liquidado da Deriv. Martingale está bloqueado.
- Contratos confirmados são consultados novamente em background e o P/L só é persistido quando a Deriv informa a liquidação.
- O botão `LIGAR SIMULAÇÃO` controla somente o simulador local; não inicia AutoPilot na Deriv. AutoPilot demo permanece desabilitado até a validação manual ponta a ponta.
- A compra demo fica desligada por padrão (`NEXORA_DEMO_BUY_ENABLED=false`). A flag pode ser habilitada apenas no ambiente de teste, depois de confirmar conta demo, banco persistente e guardas.
- Um resultado `UNKNOWN` sem `contract_id` bloqueia novas compras. A recuperação automática desse caso ainda depende de reconciliação manual com a Deriv; nunca repetir a compra às cegas.

A confirmação visual é apresentada a cada compra manual, mas é uma proteção de interface, não uma prova criptográfica de gesto humano: uma chamada direta autenticada à API pode contornar o diálogo do navegador. Mantenha o acesso à sessão restrito e use somente a conta virtual.

## Testes locais

Python 3.11 ou superior:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt pytest
python -m pytest -q
```

Os testes usam mocks e não enviam ordens à Deriv. Para executar localmente, configure as variáveis de ambiente copiando `.env.example` e gere uma chave Fernet:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Não coloque `.env`, tokens OAuth, URI de banco ou chave Fernet no Git. O SQLite só é permitido para desenvolvimento local; uma implantação pública requer PostgreSQL persistente.

## Implantação e configuração demo

Use o serviço Render já existente e a base Postgres do Supabase. Configure `DATABASE_URL`, `NEXORA_FERNET_KEY`, `NEXORA_PUBLIC_URL`, `DERIV_REDIRECT_URI` e `DERIV_CLIENT_ID` exclusivamente nos campos de segredo/configuração do Render. O callback HTTPS precisa corresponder exatamente à URL registrada na Deriv. Mantenha uma única instância/worker.

Só habilite `NEXORA_DEMO_BUY_ENABLED=true` no ambiente de validação depois de confirmar que o serviço está usando banco persistente, conta demo autenticada e controles ativos. Nenhum segredo deve ser colocado no repositório. Não habilite conta real.

Na inspeção pré-publicação, a URL pública indicada respondia como versão 0.4 e `/api/demo/capabilities` retornava 404. O serviço Render está conectado a `main` com auto-deploy; confirme `/healthz` e as rotas v0.5.2 depois da publicação. O pacote não comprova OAuth, compra, liquidação ou histórico oficial até que isso seja observado na conta Demo autenticada.

## Limitações

A simulação local e o histórico Deriv Demo são fontes distintas. A simulação usa regras experimentais e não demonstra rentabilidade. O serviço de teste não é infraestrutura para dinheiro real; planos que hibernam/reiniciam podem atrasar as consultas de liquidação. Tokens Deriv expiram e exigem reconexão. Não existe AutoPilot demo habilitado neste pacote.

## Documentação oficial consultada

- [OAuth 2.0 Deriv](https://developers.deriv.com/docs/intro/oauth/)
- [Options Setup e OTP](https://developers.deriv.com/docs/options/)
- [WebSocket Demo](https://developers.deriv.com/docs/options/ws-demo/)
- [Fluxo completo](https://developers.deriv.com/docs/workflows/)
- [Proposal](https://developers.deriv.com/docs/trading/proposal/), [Buy](https://developers.deriv.com/docs/trading/buy/) e [Open Contract Status](https://developers.deriv.com/docs/trading/proposal-open-contract/)
