# NEXORA IA — estado da continuidade

**Revisão local:** 12 de novembro de 2026, v0.5.2. O histórico fornecido tinha HEAD `653b57c`; a revisão local já foi commitada, mas o commit ainda não havia sido enviado quando este estado pré-push foi registrado.

A suíte local passou com **44 testes** e um aviso de depreciação do Starlette/httpx. O JavaScript passou `node --check` e `git diff --check` não encontrou whitespace inválido. Os testes usam mocks; nenhum login Deriv ou contrato foi executado.

O serviço Render `Nexoria-Ia` (região Oregon, plano Free) usa o repositório privado `nexoriatrader/Nexoria-Ia`, branch `main`, com auto-deploy em novos commits. Antes da publicação, a URL pública respondeu `version: 0.4` e `GET /api/demo/capabilities` retornou 404. A variável `NEXORA_DEMO_BUY_ENABLED` foi definida explicitamente como `false` por merge, sem ler ou substituir outras variáveis; o deploy de configuração concluiu `live` no commit anterior `653b57c`. O push de v0.5.2 acionará o deploy de código; valide `/healthz` e as rotas antes do login.

O pacote v0.5.2 inclui saldo/moeda Demo, proposal read-only, compra manual com reserva idempotente, limite por conta e reconciliação automática somente de contratos confirmados. Compra real e AutoPilot Deriv permanecem bloqueados. `UNKNOWN` sem `contract_id` bloqueia novas entradas e requer revisão manual.

O saldo/liquidação oficiais, compra supervisionada e histórico ainda precisam ser comprovados na conta Demo autenticada. Nenhum segredo de produção está no pacote; `.env`, chave Fernet, credenciais Supabase, tokens OAuth e cookies não foram incluídos. Leia `ATUALIZACAO_V052.md` e `DERIV_DEMO_INTEGRATION.md`.
