# NEXORA IA — estado da continuidade

**Revisão local:** 12 de novembro de 2026, v0.5.2. O histórico Git foi reconstruído do bundle fornecido, cujo HEAD anterior era `653b57c`; as mudanças desta revisão ainda não foram publicadas no remoto.

A suíte local passou com **44 testes** e um aviso de depreciação do Starlette/httpx. O JavaScript passou `node --check` e `git diff --check` não encontrou whitespace inválido. Os testes usam mocks; nenhum login Deriv ou contrato foi executado.

A URL pública `https://nexoria-ia.onrender.com` ainda respondia `version: 0.4` e `GET /api/demo/capabilities` retornou 404. O pacote v0.5.2 inclui consulta de saldo/moeda demo, proposta read-only, compra manual com reserva idempotente, limite por conta e reconciliação automática somente de contratos confirmados. `NEXORA_DEMO_BUY_ENABLED` permanece desligado por padrão; o executor real e o AutoPilot Deriv continuam bloqueados.

Um resultado `UNKNOWN` sem `contract_id` continua bloqueando novas entradas e requer revisão manual na Deriv. O saldo/liquidação oficiais, compra supervisionada e histórico ainda precisam ser comprovados na conta Demo autenticada depois que a versão atualizada estiver publicada. Nenhum segredo de produção está neste pacote; `.env`, chave Fernet, credenciais Supabase, tokens OAuth e cookies não foram incluídos.

Leia `ATUALIZACAO_V052.md` e `DERIV_DEMO_INTEGRATION.md` para os detalhes técnicos e as limitações.
