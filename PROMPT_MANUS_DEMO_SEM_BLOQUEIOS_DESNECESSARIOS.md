# PROMPT PRINCIPAL — NEXORA IA: AVANÇAR COM DERIV DEMO, SEM TRAVAS EXCESSIVAS

Você é o agente Manus responsável por continuar o NEXORA IA a partir deste código. A intenção expressa do proprietário é **testar o robô usando a conta de TREINAMENTO oficial da Deriv**, com saldo virtual, e prosseguir até obter evidências de execução, liquidação e histórico. NÃO interprete o bloqueio de dinheiro real como proibição de operar no ambiente DEMO.

## Regras que são obrigatórias

1. **Treinamento:** disponibilizar a conta demo autenticada, saldo demo quando autorizado pela API, consulta de proposta e **teste supervisionado de compra demo**, com saldo virtual. Este fluxo deve ser desenvolvido e executado assim que houver login/autorização demo e condições técnicas válidas.
2. **Real:** opção pode aparecer na UI, identificada como indisponível para compras nesta fase. Não permitir rota, WebSocket ou credencial de conta real no executor. Não executar compras reais. Elegibilidade/regulação serão avaliadas antes de qualquer futuro lançamento.
3. **Teste manual não é AutoPilot:** antes de ligar compras automáticas, confirmar um ciclo demo completo: proposta → compra → contrato confirmado → liquidação → P/L OFICIAL no banco e painel.
4. **Não criar obstáculos artificiais:** a flag `NEXORA_DEMO_BUY_ENABLED` deve ser ativada como `true` no ambiente de desenvolvimento/validação demo, depois de revisar a configuração e com autorização consciente do usuário autenticado. Ela NÃO controla conta real. A UI tem botão específico `TESTAR COMPRA DEMO`, que pede confirmação antes de cada envio. É apropriado usar contratos demonstrativos para verificar integração.
5. **Guardas obrigatórias, não opcionais:** identificador idempotente único; reserva no banco antes da chamada remota; apenas 1 contrato ativo ou pendente por usuário; Stop Loss/Stop Win; nunca repetir compra de resultado desconhecido; reconciliar com a Deriv; isolamento por conta; registrar erros sem segredos.
6. **Teste de Martingale:** inicialmente desligado para compra demo, mas implemente-o de modo progressivo e verificável na fase seguinte, respeitando exposição máxima/Stop Loss e limite de progressões. Não usar Martingale para recuperar prejuízos de forma ilimitada.
7. **Não simular sucesso:** operações virtuais e Deriv Demo são fontes diferentes; P/L da Deriv só após confirmação/liquidação da API. Relatar qualquer bloqueio técnico com evidência e próxima correção.
8. **UI para usuário final continua simples:** login, escolha treinamento/real, stake, Martingale opcional, Stop Win/Stop Loss, botão LIGAR/DESLIGAR, histórico. Botões técnicos de compra manual são temporários para validação e depois podem migrar para página de testes de administrador.

## Ação imediata, sem pedir autorização repetitiva

- Restaurar o projeto, rodar `pytest -q`, verificar `.env.example` e configurar no Render as chaves secretas através da interface segura (NUNCA adicionar segredos ao GitHub).
- Conferir conta demo pela autenticação oficial Deriv. Se o desenvolvedor não tiver sessão autenticada, solicitar UMA vez o login pelo usuário, não pedir senhas/tokens no chat.
- Definir `NEXORA_DEMO_BUY_ENABLED=true` **somente no ambiente de teste**, registrar o ambiente e nunca configurar compra real. Os testes de compra requerem confirmação explícita na tela do usuário antes de enviar a ordem.
- Usar `/api/demo/capabilities` para conferir disponibilidade. Na tela, usar `TESTAR COMPRA DEMO`. Consultar `GET /api/demo/orders` e `POST /api/demo/orders/{id}/reconcile` até contrato liquidado.
- Corrigir erros de integração (símbolo, moeda, duration, esquema API, OTP, credenciais de demonstração), atualizando testes e registrando evidências.
- Completar reconciliação automática de contratos confirmados, persistência oficial, botão LIGAR/DESLIGAR para AutoPilot DEMO sob limites e parada segura. Habilitar AutoPilot DEMO **somente após** testes ponta a ponta e guardas aprovados, sem exigir confirmação humana para cada entrada automática após o usuário ligar conscientemente o modo demo.
- O cliente deve poder desligar novas entradas a qualquer momento. Reinícios da Render não podem causar compra repetida ou ativação silenciosa.
- Entregar commit, URL, testes, evidências demo, limitações e pendências, sem proclamar perfeição não comprovada.

## Infra já criada

- GitHub: https://github.com/nexoriatrader/Nexoria-Ia
- Render (painel): https://dashboard.render.com/web/srv-dbqd9rdg1s2s73e0difg
- URL pública indicada no backup: https://nexoria-ia.onrender.com
- Supabase project ID: xpkulrzwkecnwvxzymyq
- Deriv Client ID (público): 34VU8L3LDiaENrDGVjqQP
- OAuth callback: https://nexoria-ia.onrender.com/auth/deriv/callback

**Não há nenhum motivo para deixar a conta de treinamento eternamente bloqueada.** Habilitar e testar o fluxo demo de forma controlada faz parte do trabalho. A liberação de operações com dinheiro real é um projeto separado.
