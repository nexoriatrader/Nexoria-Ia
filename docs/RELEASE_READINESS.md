# NEXORA — checklist obrigatório de lançamento

Atualização inicial: 2026-11-17.
Não marcar "pronto" apenas porque o deploy aparece como live.

## Bloqueadores observados
- [ ] GitHub Actions `NEXORA Python tests`: último main = 38 failed, 205 passed.
- [ ] Resolver divergências reais/legadas de testes sem apagar os controles de risco.
- [ ] Supabase: 27 tabelas públicas com RLS desligado e permissões de clientes excessivas.
- [ ] Executar e testar a migração de segurança descrita em `RELEASE_SECURITY_RLS.md`.
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
