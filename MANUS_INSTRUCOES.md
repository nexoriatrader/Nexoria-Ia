# INSTRUÇÕES PARA O MANUS — NEXORA IA

## Contexto
Você recebeu o repositório `nexora-ia` v0.4. O usuário quer uma experiência muito simples: 1) login/cadastro via Deriv, 2) selecionar conta de treinamento ou real (real desabilitada), 3) informar valor da entrada (mínimo US$ 2 conforme configuração interna, sujeito ao contrato), Martingale opcional e multiplicador, Stop Win e Stop Loss, 4) painel com grande botão de ligar/desligar, resultado e histórico. Toda seleção de ativo, estratégia e expiração deve ser interna.

App ID Deriv (público): `34VU8L3LDiaENrDGVjqQP`.
Redirect local registrada: `http://localhost:8000/auth/deriv/callback`.
Nome oficial desejado: NEXORA IA; cadastro Deriv pode ter sido feito como Nexoria IA.
Existe link de afiliado fornecido ao projeto, mas **não inserir o link automaticamente** em funil de robôs sem validar autorização contratual e jurisdição de divulgação.

## O que já existe
- FastAPI com login OAuth2 + PKCE, sessões opacas, chaves secretas fora do código.
- Consulta demonstrativa conservadora e registro individual no banco.
- `DATABASE_URL` (Supabase Postgres), `render.yaml`, `.env.example`, tela HTML.
- Stream de preço público; simulador tick-based; consulta read-only de contrato e proposta Deriv.
- Rotas de compras reais ou demonstrativas **não existem**.
- Testes com pytest.

## Fase A — Implantar apenas ambiente de teste
1. Configurar GitHub privado e Render com o Blueprint. NÃO fazer commit de segredos.
2. Criar e configurar Supabase PostgreSQL. Preparar migrações Alembic.
3. Verificar HTTPS no Render, callback configurada no painel Deriv, OAuth real, retorno da API e identificação correta da conta demo.
4. Garantir testes de dois usuários simultâneos sem vazamentos de sessão/histórico. Validar sessões expirada/revogadas.
5. Revisar CSRF, segurança e limites antes de liberar a URL para usuários externos. Construir logs e painel de operação.

## Fase B — Execução autêntica em DEMO, nunca real
1. Pesquisar/validar endpoints no schema e documentação vigentes, não confiar cegamente em exemplos antigos.
2. Obter URL WebSocket autenticada por OTP para **conta DEMO identificada**. Aplicar checagem dupla no backend de tipo de conta.
3. Implementar cotação por `proposal`, compra `buy` autorizada **somente demo** e verificação `proposal_open_contract` com tickets reais da conta demo.
4. Ter `client_order_id` interno, estado `PENDING/CONFIRMED/SETTLED/UNKNOWN`, fila durável e reconciliação de ordens ambíguas após timeout. Se não houver identificador/idempotência do provedor, não repetir uma compra de status desconhecido automaticamente.
5. Stops são pré-checagens do risco máximo de perda incluindo operações abertas e progressões; não prometer garantia absoluta de stop em falhas. Limitar a 1 operação por conta, e 2 gales no máximo conforme planejamento.
6. Enquanto houver falhas de conexão ou ordem não reconciliada, bloquear novas entradas; sempre preservar histórico auditável.
7. Testar toda operação com conta demo acessível autorizada; salvar evidência de ordens no histórico oficial da Deriv (não resultados inventados).

## Fase C — Inteligência
1. Testar tendência, reversão e rompimento separadamente. Não alegar taxa de acerto sem backtest e validação out-of-sample.
2. Calcular break-even de cada proposta com lucro/pagamento real; avaliar custos e condições.
3. Criar Shadow Lab separado em paper trading; mudanças de estratégia exigem validação, nunca promover automaticamente um modelo recém-treinado a live.
4. Testes por ativo, horário e expiração; proteger contra viés de seleção/look-ahead/data leakage.

## Fase D — Opcional, após autorizações
- Não criar modo REAL nem funil de afiliados para brasileiros sem avaliações jurídicas e de elegibilidade pertinentes.
- Assinaturas (futuro): 7 dias grátis desde a primeira ativação, R$ 29,90/15 dias, R$ 49,90/mês, R$ 99/trimestre. Não implementar agora; foco no robô.

## Critérios de aceite
- OAuth responde corretamente no domínio Render e recusa callback inválida.
- Reinício nunca religa bot ou duplica ordem.
- Usuário A não consegue alterar/ver dados do usuário B.
- Conta real não permite nenhuma ordem em TODO o código e em todos os endpoints.
- Saldos/resultados exibidos como DEMO DERIV só quando realmente provenientes de API autenticada; simulação virtual claramente separada.
- Testes automatizados e documentação com passos reproduzíveis.
- Entregar ZIP completo e repositório com instruções de inicialização, sem tokens/segredos.


## INFRAESTRUTURA JÁ CRIADA (usar, não recriar)

**Leia primeiro `INFRAESTRUTURA_MANUS.md`**. Repositório: https://github.com/nexoriatrader/Nexoria-Ia ; Supabase: https://supabase.com/dashboard/project/xpkulrzwkecnwvxzymyq ; Render existente: https://dashboard.render.com/web/srv-dbqd9rdg1s2s73e0difg . Endereço HTTPS público ainda não foi informado.
