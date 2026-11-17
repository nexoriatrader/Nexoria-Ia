# NEXORA REAL — o usuário define o risco

Decisão funcional: não impor tetos arbitrários de entrada, perda diária
nem quantidade de operações no modo REAL.

- **Valor de cada entrada:** definido pelo usuário na configuração, respeitando
  mínimos técnicos e valores aceitos pela Deriv.
- **Stop Loss:** definido pelo usuário e obrigatório para ativar o robô REAL.
  O ledger usa o prejuízo oficial acumulado do dia (fuso America/Sao_Paulo)
  e impede nova entrada quando o limite for atingido, ou quando a exposição
  da próxima entrada ultrapassaria esse limite.
- **Stop Win:** definido pelo usuário; se zero, desativado.
- **Quantidade diária de operações:** sem teto arbitrário imposto pelo NEXORA.
  Operações só continuam enquanto houver autorização individual, saldo
  suficiente, sinal válido, limites de risco permitirem e nenhuma transação
  anterior estiver pendente ou incerta.
- **Sem Martingale** nesta fase do modo REAL. Nenhuma compra é realizada sem
  ativação manual e confirmação explícita de risco.
- **Apenas uma operação ativa/incerta por conta**; compras incertas bloqueiam
  novas entradas até a reconciliação.

Os antigos `NEXORA_REAL_MAX_STAKE_USD`,
`NEXORA_REAL_MAX_DAILY_LOSS_USD` e `NEXORA_REAL_MAX_DAILY_ORDERS`
foram descontinuados. Mesmo que persistam no serviço, não participam
das decisões de risco do código atualizado.

A opção REAL depende de duas chaves administrativas de habilitação do
servidor e do consentimento explícito do próprio usuário. Durante a publicação
da atualização, manter a chave de aprovação desativada para evitar operações
com uma versão de código ainda antiga.

Nenhum controle garante lucro ou limita perdas com precisão absoluta caso
ocorra atraso, rejeição, erro de execução ou discrepância de liquidação.
