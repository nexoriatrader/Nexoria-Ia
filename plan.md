# Plano — Nexora Manual e Catalogador de Sinais

## Objetivo e abordagem — Nexora Manual
Adicionar uma modalidade explícita `Nexora IA Estratégias` ou `Nexora Manual (lista de sinais)` ao perfil salvo, preservando integralmente o fluxo atual de Estratégias. No modo Manual, a configuração mantém conta, stake, stops, payout e proteções, mas não solicita estratégia, mercado nem ativos antecipadamente. O mercado é escolhido por lista no card do painel.

O card terá prévia sem execução, entrada de uma ou várias linhas, escolha entre hoje/amanhã, mercado e fuso IANA (padrão `America/Sao_Paulo`, rotulado claramente como Brasília UTC−03:00). O catálogo oficial Deriv será usado para normalizar aliases reconhecidos e classificar cada ativo. Erros de formato, horário passado e ativo desconhecido serão mostrados linha a linha; ativos de outro mercado serão explicitamente excluídos sem descartar os sinais válidos restantes. A confirmação agenda apenas itens válidos e informa os excluídos. Quando todos os sinais tiverem resultado definitivo, o card mantém uma ação clara de “DESLIGAR ROBÔ”, mesmo diante de status momentâneo incompleto; o desligamento preserva a fila e não cancela contratos enviados.

A fila será persistida em tabelas próprias: execução/sessão manual, sinais individuais e recibos por lote. A data local, fuso, mercado, ativo canônico, direção, duração, horário UTC de entrada, estado, motivo, chave idempotente e vínculo com o ledger serão guardados. Cada lote persiste também hash do payload e resposta confirmada sob chave única por usuário/sessão, permitindo retry idempotente após pausa/reinício e recusando a mesma chave com conteúdo diferente. A interface conserva a chave de retry no `localStorage` até uma resposta definitiva. Cada lista pertence a um mercado. `M15` é a duração do contrato, que só começa quando a Deriv confirma a compra; o horário no texto é a entrada prevista. Uma linha só pode ser tentada durante o minuto indicado; depois disso é marcada como perdida, sem tolerância extra, recuperação ou reenvio. Sinais futuros permanecem persistidos ao pausar; reinícios deixam a execução pausada para reativação explícita.

O teto é de 100 sinais pendentes por sessão manual ativa. A vaga libera quando o sinal tem desfecho definitivo na fila (compra confirmada, perda da janela ou encerramento sem compra); uma tentativa incerta permanece ocupando vaga até reconciliação oficial. A vaga do agendamento não libera o slot de ordem do ledger: contratos abertos/incertos continuam protegidos pelas regras atuais da corretora. Mudanças de modalidade/conta são bloqueadas enquanto há sinais agendados, em tentativa ou incertos; as operações de alteração de perfil e confirmação usam travas/serialização para proteger a identidade da execução.

A execução DEMO passará exclusivamente por `perform_demo_buy()` e `demo_ledger`; a execução REAL reutilizará a ativação/consentimento existentes, os limites e reservas de `real_ledger`, a validação de proposta e `buy_real_contract()` e a reconciliação REAL. Prévia e agendamento não enviam ordens. Testes usarão mocks e não contactarão as APIs de compra.

## Escopo — Catalogador de Sinais
Adicionar uma página autenticada “Catalogador” à navegação desktop e móvel. A primeira versão será de pesquisa, somente leitura, sem iniciar operações. O fluxo principal deve exigir poucos cliques: escolher mercado e ativo Deriv, duração/candle M5 ou M15 e hoje/amanhã; usar Brasília UTC−03:00 como fuso inicial e permitir fuso disponível do módulo Manual. No modo Hoje, listar somente horários ainda futuros. O histórico padrão será 60 dias, selecionável para 30 dias.

A primeira estratégia será denominada **Viés histórico por horário**. Para cada horário local e candle fechado, a ferramenta observa direções históricas do candle seguinte/slot equivalente; a direção candidata é a maioria histórica desse horário no segmento de treino. Candidatos são selecionados apenas no segmento de treino (mínimo de 20 observações direcionais e taxa histórica mínima de 70%), e a direção exportada também é calculada exclusivamente com esses dados. A validação walk-forward usa, em cada dia testado, somente observações anteriores; exige ao menos 8 observações para reportar uma taxa. Esse piso fixo de suficiência de dados não depende do desempenho: a taxa walk-forward não escolhe, ordena, elimina nem altera a direção dos candidatos — isso impede vazamento do próprio bloco fora-da-amostra para a lista. Não usar velas incompletas. Padrões doji/neutros ficam fora do denominador direcional. Limitar a 5 candidatos por dia; se não houver amostra ou candidatos que atendam aos critérios, explicar o motivo em vez de inventar sinais. Mostrar por sinal os números de amostras, acertos e taxas históricas/validação; a interface deve esclarecer que isso não é garantia nem probabilidade calibrada.

A saída deve poder ser copiada como linhas `M5 <ativo> HH:MM CALL/PUT` ou `M15 ...`, compatíveis com o pré-visualizador manual. O botão copia texto; não agenda nem envia sinais automaticamente. Todos os símbolos e dados vêm do catálogo e do endpoint histórico público da Deriv, com janela de consultas limitada, paginação e cache para evitar excesso de chamadas. A simulação de retorno financeiro/payout líquido não faz parte deste MVP: o resultado mede direção de candles, não lucro de contrato.

## Estrutura do projeto
- `app/index.html`: navegação para a página Catalogador, formulário simples, resultados auditáveis, cópia de lista e responsividade móvel.
- `app/main.py`: endpoints autenticados de listagem de ativos e geração; ambos somente leitura e separados do executor/ledger.
- `app/signal_cataloger.py`: busca paginada e limitada de candles públicos, agregado por hora/fuso e avaliação walk-forward, sem dependência da execução de ordens.
- `app/market.py` e `app/automation_markets.py`: cliente e catálogo Deriv existentes, reutilizados.
- `app/manual_signals.py`: gramática vigente de listas manuais para validar o formato exportado.
- `tests/`: testes isolados de algoritmo, timezones, paginação/limites, endpoint autenticado e regressão das rotas manuais.

## Interface e estilo
Reutilizar o painel Nexora existente: cartões escuros em azul-marinho, bordas azul-petróleo, destaque dourado para ações principais, hierarquia tipográfica e componentes responsivos já presentes. O card prioriza estados legíveis, explicações por linha e confirmação inequívoca de conta REAL; não introduzir imagens ou dependências visuais novas.

## Restrições materiais
- Sem novas dependências externas e sem alteração destrutiva dos ledgers ou da agenda de estudo.
- Nenhuma tentativa antes de confirmar a lista; nenhum envio de ordem durante testes.
- Estado do robô e capacidade da fila devem vir do banco, não apenas de memória de processo.
- A agenda não pode transformar sinal vencido em compra tardia ou duplicada.
- O Catalogador não chama endpoints de compra, não altera o modo/conta do usuário e não se anuncia como previsão garantida.
