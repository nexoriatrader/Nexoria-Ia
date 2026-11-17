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


## Diagnóstico da conta conectada (17/11/2026)

Consulta direta `GET /v1/account` feita na única conexão Stripe disponível:
- Conta BR em **modo de testes**, id `acct_1UcP5o0qx5vSGQtt`.
- `charges_enabled=false`; `payouts_enabled=false`;
  `details_submitted=false`; `requirements.disabled_reason=requirements.pending_verification`.
- Nenhum Checkout Session e nenhum produto encontrado nesse ambiente de testes.
- A conta não está pronta para cobranças LIVE; nenhum pagamento real foi criado.

**Aceitação de atividades financeiras:** NEXORA oferece análise e automação de
contratos digitais/opções na Deriv. O comerciante deve declarar esse modelo
integralmente à Stripe e obter aprovação adequada antes de ativar produção.
Conforme a política da Stripe Brasil, serviços financeiros e de investimento
podem exigir diligência adicional, licença ou autorização escrita.
Não classificar como SaaS genérico omitindo automação de trading.

Fontes:
- https://stripe.com/br/legal/restricted-businesses
- https://support.stripe.com/questions/brazil-specific-information-on-the-types-of-businesses-products-and-industries-that-can-t-use-stripe-s-services?locale=pt-BR

## Ciclo de vida e retorno do Checkout

- Os três preços, períodos e moeda BRL são geridos no backend.
- `POST /api/billing/checkout`: usuário com sessão Deriv válida cria
  sessão hospedada pela Stripe; servidor grava status inicial `PENDING`.
- `GET /api/billing/checkout/status?session_id=...`: somente o dono da
  sessão pode consultar `PENDING`, `PAID`, `FAILED` ou `EXPIRED`.
- Retorno `?payment=success` **não libera acesso**: a aplicação consulta
  o banco; apenas o webhook assinado com `payment_status=paid`,
  valores, moeda, usuário e plano idênticos libera tempo.
- Eventos `checkout.session.completed` e
  `checkout.session.async_payment_succeeded` confirmam acesso pago,
  quando de fato liquidados.
- Eventos `checkout.session.expired` e
  `checkout.session.async_payment_failed` marcam checkouts pendentes
  como expirados/falhos. Um evento tardio **não remove um pagamento PAID**.
- Eventos desconhecidos são ignorados, sem permitir compra ou crédito.
- Testes simulados não usam chave real nem movimentam recursos.

## Pendências antes de cobrar

1. Concluir dados e verificação comercial no Dashboard Stripe,
   informando atividade financeira/automação de negociações de forma precisa.
2. Obter confirmação da Stripe de que aceita o NEXORA no modo LIVE.
   Caso não aceite, escolher outro prestador **de forma transparente e
   conforme a política do prestador**, sem ocultar a atividade.
3. Garantir webhook LIVE assinado e correto. Para acompanhamento do status
   e marcação de falha/expiração usar os quatro eventos acima.
4. Desenvolver e testar o fluxo de reembolso/contestação e política de
   recuperação de acesso. **Ainda não há revogação automática de dias
   por reembolso**, portanto não ativar LIVE sem tratamento operacional.
5. Validar checkout real supervisionado somente depois da aprovação,
   confirmação de chaves, suporte e autorização aplicável.
