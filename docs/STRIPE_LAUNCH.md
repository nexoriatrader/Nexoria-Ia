# NEXORA — habilitação segura da cobrança Stripe

Preparação de lançamento, 17/11/2026. Sistema de acesso **pré-pago e não
recorrente**; a Stripe não renova a assinatura automaticamente.

## Planos fixos no backend
- R$ 29,90 / 15 dias
- R$ 49,90 / 30 dias
- R$ 99,00 / 90 dias
- 7 dias de teste para novos usuários, sem cobrança automática

## Separação de ambientes
A configuração agora exige `NEXORA_STRIPE_MODE=test|live` explicitamente,
`NEXORA_STRIPE_BILLING_ENABLED=true`, `STRIPE_SECRET_KEY` do modo
correspondente e `STRIPE_WEBHOOK_SECRET` da mesma conta/ambiente.
Em sites HTTPS públicos, o NEXORA impede o modo de testes de creditar
assinaturas; somente configuração LIVE poderá funcionar. Sem chaves válidas,
o recurso fica indisponível.

O backend valida o campo `livemode` da sessão Checkout e o do evento
Stripe, além de assinatura HMAC, prazo de 5 minutos, usuário, pedido interno,
plano, moeda BRL, valor, `payment_status=paid` e idempotência.

## Antes de aceitar pagamentos
1. Habilitar conta Stripe LIVE e finalizar exigências cadastrais/tributárias
   aplicáveis. A conexão atual usada na auditoria só disponibilizava TEST.
2. No Render, configurar `NEXORA_STRIPE_MODE=live` e os segredos LIVE por
   variáveis privadas, nunca em GitHub ou no código.
3. Criar webhook LIVE para:
   `https://nexoria-ia.onrender.com/api/billing/stripe/webhook`,
   com eventos `checkout.session.completed` e
   `checkout.session.async_payment_succeeded`.
4. Validar uma transação live supervisionada e eventual estorno/cancelamento,
   crédito exato de dias, reenvio duplicado do evento e falhas HTTP 503.
5. Confirmar tratamento de reembolsos/disputas e política de acesso dos
   clientes; atualmente o backend não tem rotina de revogação automática por
   reembolso/disputa.
6. Manter o recurso fechado ao público se não houver plano de suporte,
   reembolso e obrigação legal corretamente definidos.

**IMPORTANTE:** testes automatizados são offline e não movimentam dinheiro;
não demonstram que o Checkout LIVE está pronto.
