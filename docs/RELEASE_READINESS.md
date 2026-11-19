# NEXORA — checklist obrigatório de lançamento

Atualização inicial: 2026-11-17. Revisão factual: 2026-11-19.
Não marcar "pronto" apenas porque o deploy aparece como live.

## Bloqueadores observados
- [x] PR #31 incorporada à `main` em 17/11/2026. No HEAD `d51aedba` (18/11/2026), GitHub Actions `NEXORA Python tests`, auditoria de segredos e smoke público concluíram com sucesso. O total histórico de 260 testes aprovados refere-se à PR #31; não confundir com contagem da execução atual.
- [x] Ajustar e validar testes de regressão do fluxo V2 e regras de risco na PR #31.
- [x] Supabase: migração `20261117181651` registrada como aplicada. Consulta SQL read-only em 19/11/2026 confirmou **28/28** tabelas `public` com RLS e zero tabelas com `SELECT` ou `INSERT` direto para `anon` ou `authenticated`.
- [x] SQL de segurança aplicado e confirmado por contagens e Security Advisor; acesso direto do papel `postgres` preservado.
- [ ] Validar login/dashboard em ambiente real após migração e desabilitar no Dashboard Supabase a opção `Automatically expose new tables`.
- [ ] Configurar proteção obrigatória da branch main (GitHub Pro ou plano elegível); repositório continua privado no GitHub Free.
- [x] Habilitar auditoria de segredos no CI da PR #31: `NEXORA repository security audit` aprovado. Não cobre todo o histórico de commits.
- [x] Código REAL endurecido: exige simultaneamente `NEXORA_REAL_AUTOPILOT_ENABLED=true` e `NEXORA_REAL_RELEASE_APPROVED=true`; ambos ficam `false` por padrão na ausência de configuração, mesmo após deploy.
- [ ] Confirmar no Render as habilitações administrativas do modo REAL, sem alterá-las durante a auditoria; a PR #31 já foi integrada à `main`. Os valores efetivos do ambiente não foram auditados.
- [ ] Validar conta real com autenticação Deriv, consentimento explícito e compatibilidade da conta; NUNCA fazer compras de produção em teste automatizado.
- [ ] Rever comunicação de status entre `/healthz`, `/api/session`, `/api/accounts/available`, perfil e configuração inicial.
- [x] Implementar heartbeat da Deriv e sinalização ONLINE somente após preço real, com testes de recuperação por mocks na PR #31.
- [ ] Verificar nos logs e métricas do Render se houve redução de `ConnectionClosedError` após a integração da PR #31. O merge no GitHub não confirma a estabilidade do deploy.
- [ ] Testar idempotência, contrato UNKNOWN, liquidação, saldo insuficiente, reconexão, reinício e limites de Stop Loss/Stop Win.
- [x] Separar Stripe TEST/LIVE por variáveis explícitas, validar modo de sessão/evento e bloquear créditos de teste em ambiente público, com testes.
- [ ] Confirmar conta Stripe LIVE apta para a atividade econômica, webhook e chaves live; testar transação supervisionada, estorno e evento duplicado sem exposição de segredos.
- [ ] Validar 7 dias gratuitos, expiração de acesso, e planos na tela.
- [ ] Testar 10 usuários simultâneos em DEMO com dados compartilhados, p95, CPU, RAM, conexões DB e limites da Deriv, sem compras reais.
- [ ] Preparar monitoramento de erros, alarmes, backups/restauração, contatos de suporte e regras de liberação gradual.
- [ ] Confirmar aspectos contratuais, regulatórios e mensagens de risco antes de comercialização.

## Regras de corte
1. Impedir lançamento público se os testes do CI falharem.
2. Não usar `NEXORA_REAL_AUTOPILOT_ENABLED=true` até aprovação de homologação da integração.
3. Testes em produção não devem criar contratos reais ou prometer lucratividade.
4. Publicação do Render só após revisão/merge aprovado; branch de correções não é deploy.
5. Aprovar piloto de poucos usuários após revisão de segurança e autenticação; expandir gradualmente.

## Auditoria de continuidade — 19/11/2026
- [x] Consultado HEAD da branch `main` (`d51aedba`): testes Python, auditoria de segurança e smoke público com conclusão `success` na última execução relevante consultada (18/11/2026).
- [x] Consultados privilégios PostgreSQL via SQL read-only: 28/28 tabelas com RLS; 0 `SELECT` e 0 `INSERT` para `anon` / `authenticated`.
- [x] Consultados Supabase Advisors: avisos apenas informativos (`rls_enabled_no_policy` e `unused_index`); não foram aplicadas alterações de esquema.
- [ ] Revisar a expansão do smoke público sem autenticação para rotas privadas (`/api/status`, `/api/automation/profile`, `/api/demo/orders`, `/api/real/orders`). A verificação deve ser somente leitura e exigir `401` e `Cache-Control: no-store`.
- [ ] Validar acesso autenticado e isolamento entre dois usuários; os testes de resposta anônima não substituem testes de autorização por proprietário.
- [ ] Medir latência p50/p95, tempo de despertar Render Free, RAM/CPU e concorrência para o painel, sem disparar operações.
- [ ] Confirmar backups/restauração e monitoramento operacional antes de qualquer decisão de lançamento.

Relatório detalhado: `docs/READINESS_AUDIT_2026-11-19.md`.
Nenhuma verificação deste checklist concede autorização para executar contratos reais.
