# NEXORA — auditoria da landing, autenticação Deriv e onboarding

Data: 17/11/2026. Correções de primeira indicação de usuário.

## 1. Condição do teste

O trial padrão de novas contas é **24 horas** e é criado quando o primeiro
OAuth Deriv é concluído com sucesso (não ao ligar o robô).

- O backend lê `NEXORA_TRIAL_HOURS` e o Render deve estar configurado
  explicitamente com `24`.
- A landing obtém a duração de `GET /api/public/offer`, sem autenticação,
  para evitar divergência entre o ambiente e os textos estáticos.
- A indicação validada soma **24 horas** ao acesso do indicador.
- A cobrança prevista é pré-paga, sem renovação automática, com planos
  15/30/90 dias. A landing informa que o pagamento depende de habilitação.
- **Nenhum acesso já concedido é alterado**. A auditoria do Supabase mostrou
  registros legados identificados como `trial_24h` com intervalos de
  aproximadamente 6–7 dias. Resolver esses dados exigiria revisão separada
  antes de qualquer ajuste de prazo.

## 2. Retorno repetido da Deriv

O estado e código PKCE são de uso único. A primeira autenticação exclui
a autorização pendente. Se o navegador abrir novamente a URL de retorno,
o backend verifica **somente uma sessão existente e válida**:

- Com sessão válida: redireciona à aplicação sem refazer troca OAuth.
- Sem sessão: redireciona à landing com mensagem de login expirado
  ou cancelado e permite iniciar novo OAuth.
- Nunca cria sessões a partir de estado inexistente, inválido ou expirado.
- O início de novo login limpa da base autorizações pendentes com mais de
  15 minutos, sem afetar fluxos mais recentes.
- A navegação de retorno do Android libera novamente o botão de autenticação.

## 3. Configuração em etapas

O parâmetro `/?login=ok`, entregue após login válido, abre
`robot-config`, inicializa o wizard em **7 etapas** e consulta o perfil
existente. Não envia mais o usuário ao formulário `setup` antigo.

O perfil e os limites salvos não são substituídos pelo redirecionamento.
Uma nova configuração só é gravada após o usuário confirmar o salvamento.

## 4. Conteúdo revisado

Landing e metadados: teste 24h, início da contagem, DEMO/REAL e
consentimento, limites de risco, ausência de garantia de lucro,
efeitos do desligamento e planos previstos de acesso. Adicionadas
seções transparentes de dúvidas e planos com layout responsivo.

## 5. Homologação

- Testar primeiro login com conta Deriv DEMO usando navegador Android.
- Confirmar criação de uma sessão e um grant com duração de 24h.
- Repetir a URL do callback; a página deve abrir o NEXORA já autenticado,
  nunca retornar JSON de erro.
- Testar callback inválido sem sessão; mostrar aviso e CTA de nova conexão.
- Confirmar que `login=ok` abre o wizard de 7 etapas.
- Atualizar a página; o dashboard abre normalmente sem a tela antiga.
- Confirmar `/api/public/offer` devolve 24 horas em produção e a landing
  mostra o mesmo prazo.
- Conferir redirects, estados de cookie e avisos em navegação móvel.
- **Não enviar ordens REAL para testar autenticação ou marketing.**
