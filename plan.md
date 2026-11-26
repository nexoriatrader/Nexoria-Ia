# Plano — Nexora Manual (lista de sinais)

## Objetivo e abordagem
Adicionar uma modalidade explícita `Nexora IA Estratégias` ou `Nexora Manual (lista de sinais)` ao perfil salvo, preservando integralmente o fluxo atual de Estratégias. No modo Manual, a configuração mantém conta, stake, stops, payout e proteções, mas não solicita estratégia, mercado nem ativos antecipadamente. O mercado é escolhido por lista no card do painel.

O card terá prévia sem execução, entrada de uma ou várias linhas, escolha entre hoje/amanhã, mercado e fuso IANA (padrão `America/Sao_Paulo`, rotulado claramente como Brasília UTC−03:00). O catálogo oficial Deriv será usado para normalizar aliases reconhecidos e classificar cada ativo. Erros de formato, horário passado e ativo desconhecido serão mostrados linha a linha; ativos de outro mercado serão explicitamente excluídos sem descartar os sinais válidos restantes. A confirmação agenda apenas itens válidos e informa os excluídos. Quando todos os sinais tiverem resultado definitivo, o card mantém uma ação clara de “DESLIGAR ROBÔ”, mesmo diante de status momentâneo incompleto; o desligamento preserva a fila e não cancela contratos enviados.

A fila será persistida em tabelas próprias: execução/sessão manual, sinais individuais e recibos por lote. A data local, fuso, mercado, ativo canônico, direção, duração, horário UTC de entrada, estado, motivo, chave idempotente e vínculo com o ledger serão guardados. Cada lote persiste também hash do payload e resposta confirmada sob chave única por usuário/sessão, permitindo retry idempotente após pausa/reinício e recusando a mesma chave com conteúdo diferente. A interface conserva a chave de retry no `localStorage` até uma resposta definitiva. Cada lista pertence a um mercado. `M15` é a duração do contrato, que só começa quando a Deriv confirma a compra; o horário no texto é a entrada prevista. Uma linha só pode ser tentada durante o minuto indicado; depois disso é marcada como perdida, sem tolerância extra, recuperação ou reenvio. Sinais futuros permanecem persistidos ao pausar; reinícios deixam a execução pausada para reativação explícita.

O teto é de 100 sinais pendentes por sessão manual ativa. A vaga libera quando o sinal tem desfecho definitivo na fila (compra confirmada, perda da janela ou encerramento sem compra); uma tentativa incerta permanece ocupando vaga até reconciliação oficial. A vaga do agendamento não libera o slot de ordem do ledger: contratos abertos/incertos continuam protegidos pelas regras atuais da corretora. Mudanças de modalidade/conta são bloqueadas enquanto há sinais agendados, em tentativa ou incertos; as operações de alteração de perfil e confirmação usam travas/serialização para proteger a identidade da execução.

A execução DEMO passará exclusivamente por `perform_demo_buy()` e `demo_ledger`; a execução REAL reutilizará a ativação/consentimento existentes, os limites e reservas de `real_ledger`, a validação de proposta e `buy_real_contract()` e a reconciliação REAL. Prévia e agendamento não enviam ordens. Testes usarão mocks e não contactarão as APIs de compra.

## Estrutura do projeto
- `app/index.html`: escolha de modalidade no wizard, ocultação das etapas de mercado/ativos/estratégia no modo Manual, card de lista/agendamento/estado e botão explícito de desligamento após conclusão, além de avisos de risco/fuso.
- `app/main.py`: endpoints autenticados de prévia, confirmação idempotente e estado; verificação de perfil, acesso e ativação; integração ao startup, status e desligamento preservando a fila.
- `app/db.py`: novo campo de modalidade no perfil e tabelas aditivas de sessão, sinais e recibos de lote, compatíveis com `create_all()` em SQLite local e PostgreSQL em produção.
- `app/manual_signals.py`: parsing, normalização, resolução estrita de data/fuso e validação por mercado.
- `app/real_runtime.py`: modo Manual não aciona o scanner de estratégias; a compra REAL manual usa o caminho existente de autorização/ledger/broker e reconciliação.
- `app/automation_markets.py`: catálogo Deriv atual como fonte de classificação, sem reaproveitar a agenda de estudo `app/nexora2.html`.
- `tests/`: testes isolados de parsing, mercado, data/fuso, persistência, limite, concorrência e integração com executores totalmente mockados.

## Interface e estilo
Reutilizar o painel Nexora existente: cartões escuros em azul-marinho, bordas azul-petróleo, destaque dourado para ações principais, hierarquia tipográfica e componentes responsivos já presentes. O card prioriza estados legíveis, explicações por linha e confirmação inequívoca de conta REAL; não introduzir imagens ou dependências visuais novas.

## Restrições materiais
- Sem novas dependências externas e sem alteração destrutiva dos ledgers ou da agenda de estudo.
- Nenhuma tentativa antes de confirmar a lista; nenhum envio de ordem durante testes.
- Estado do robô e capacidade da fila devem vir do banco, não apenas de memória de processo.
- A agenda não pode transformar sinal vencido em compra tardia ou duplicada.
