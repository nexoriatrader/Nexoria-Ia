# NEXORA IA — endereços oficiais do projeto (fornecidos pelo responsável)

## Serviços já criados

| Serviço | Link / identificador | Ação do Manus |
|---|---|---|
| GitHub | https://github.com/nexoriatrader/Nexoria-Ia | Publicar código **sem segredos**, na branch `main`; checar permissões e status do repositório |
| Supabase | https://supabase.com/dashboard/project/xpkulrzwkecnwvxzymyq | Usar projeto `xpkulrzwkecnwvxzymyq` e configurar `DATABASE_URL` de forma privada |
| Render | https://dashboard.render.com/web/srv-dbqd9rdg1s2s73e0difg | Configurar o **serviço já existente** `srv-dbqd9rdg1s2s73e0difg`, evitando criar um segundo sem necessidade |
| Deriv Developers | https://developers.deriv.com/ | App ID público `34VU8L3LDiaENrDGVjqQP` |
| Deriv OAuth callback local | http://localhost:8000/auth/deriv/callback | Já cadastrado para desenvolvimento local |
| Link de afiliado Deriv | https://t.deriv.link?t=HR6MZB6Q499Z | **Não publicar nem usar para captar brasileiros** antes de confirmar termos de parceria e enquadramento regulatório |

**Nome da marca pretendida:** NEXORA IA — Seu Robô Inteligente. O registro do aplicativo na Deriv foi mostrado como **Nexoria IA**. Conferir e padronizar onde permitido.

## Atenção: ainda faltam informações que NÃO devem ser adivinhadas

- **URL pública HTTPS da Render:** a URL enviada é somente a do painel administrativo. No serviço Render, copiar o endereço externo real (ex.: `https://...onrender.com`, exemplo apenas) e configurar `NEXORA_PUBLIC_URL` com ele.
- **Callback OAuth online:** adicionar exatamente `<URL_PUBLICA_RENDER>/auth/deriv/callback` na configuração do aplicativo Deriv e colocar a mesma URL em `DERIV_REDIRECT_URI` na Render.
- **Credenciais Supabase:** copiar a connection string PostgreSQL do painel Supabase (com senha) diretamente para `DATABASE_URL` nas variáveis secretas da Render. Não mostrar no chat, nos arquivos ou nos commits. Verificar pooler, SSL e conectividade conforme configuração atual do Supabase.
- **Chave de criptografia:** gerar uma chave Fernet nova e guardá-la exclusivamente em `NEXORA_FERNET_KEY` no ambiente. Nunca usar chave de exemplo em produção.
- Confirmar as permissões do App ID na Deriv e o funcionamento OAuth PKCE em um login de teste, sem compartilhar tokens.

## Configuração esperada na Render

Este projeto é Python + FastAPI:

```
Build: pip install -r requirements.txt
Start: uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1
Health: /healthz
```

Variáveis a configurar (nomes conferidos com o código):

```
DERIV_CLIENT_ID=34VU8L3LDiaENrDGVjqQP
DERIV_REDIRECT_URI=<URL_PUBLICA_RENDER>/auth/deriv/callback
NEXORA_PUBLIC_URL=<URL_PUBLICA_RENDER>
DATABASE_URL=<CONNECTION_STRING_POSTGRES_DO_SUPABASE_SECRETA>
NEXORA_FERNET_KEY=<CHAVE_FERNET_SECRETA>
NEXORA_SYMBOL=1HZ100V
```

**Importante:** o `render.yaml` incluído pode criar outro serviço se usado como novo Blueprint. Preferir configurar o serviço Render **já criado**. A instância gratuita da Render pode hibernar ou reiniciar. Útil apenas para protótipo/simulação; não é infraestrutura confiável para ordens de dinheiro real.

## Escopo e limites

A versão enviada é a **base v0.4** com documentos de infraestrutura atualizados. Ela ainda usa simulação local com payout hipotético de 80%; não executa ordens em conta demo Deriv. A validação de OAuth e da API online continua pendente. **Não declarar o robô pronto para operar ou rentável.**

## Entrega solicitada ao Manus

1. Verificar o repositório informado e enviar nele o conteúdo deste pacote, preservando diretórios `app/` e `tests/`.
2. Conectar o serviço Render existente ao GitHub, conferir branch `main`, criar segredos, configurar Supabase e obter URL externa.
3. Atualizar callback HTTPS na Deriv e testar login demo ponta a ponta.
4. Executar testes automatizados, validar isolamento por usuário, entregar link da aplicação no ar e evidências verificáveis do que funciona.
5. Evoluir para negociação **somente em conta demonstrativa autenticada**, com histórico derivado da Deriv, sem executar contratos reais.
6. Documentar problemas, limitações e próximos passos. Evitar afirmar sucesso sem evidência.

Consulte também `MANUS_INSTRUCOES.md` para o escopo do robô, a lista de proteções e os critérios de aceite.
