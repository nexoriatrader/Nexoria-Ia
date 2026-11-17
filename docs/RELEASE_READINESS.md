# NEXORA — checklist obrigatório de lançamento

Atualização inicial: 2026-11-17.
Não marcar "pronto" apenas porque o deploy aparece como live.

## Bloqueadores observados
- [x] Na PR #31, GitHub Actions `NEXORA Python tests`: 251 passed, 0 failed. **A branch main ainda usa o commit antigo**.
- [x] Ajustar e validar testes de regressão do fluxo V2 e regras de risco na PR #31.
- [x] Supabase: RLS habilitado em 27 tabelas, com privilégios de `anon` e `authenticated` revogados. Migração `20261117181651` aplicada em produção.
- [x] SQL de segurança aplicado e confirmado por contagens e Security Advisor; acesso direto do papel `postgres` preservado.
- [ ] Validar login/dashboard em ambiente real após migração e desabilitar no Dashboard Supabase a opção `Automatically expose new tables`.
- [ ] Configurar proteção obrigatória da branch main (GitHub Pro ou plano elegível); repositório continua privado no GitHub Free.
- [x] Habilitar auditoria de segredos no CI da PR #31: `NEXORA repository security audit` aprovado. Não cobre todo o histórico de commits.
- [ ] Conferir `NEXORA_REAL_AUTOPILOT_ENABLED` no ambiente Render sem revelar o valor das credenciais.
- [ ] Validar conta real com autenticação Deriv, consentimento explícito e compatibilidade da conta; NUNCA fazer compras de produção em teste automatizado.
- [ ] Rever comunicação de status entre `/healthz`, `/api/session`, `/api/accounts/available`, perfil e configuração inicial.
- [ ] Restaurar estabilidade do feed Deriv: o Render registrou `ConnectionClosedError` de candles.
- [ ] Testar idempotência, contrato UNKNOWN, liquidação, saldo insuficiente, reconexão, reinício e limites de Stop Loss/Stop Win.
- [ ] Verificar billing Stripe: flag `NEXORA_STRIPE_BILLING_ENABLED`, chave e webhook configurados (sem expor segredos); validar compra, renovação, estorno e evento duplicado em modo teste.
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
