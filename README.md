# ✂️ Clipper

Cola um link do YouTube (ou escolhe um vídeo do computador) e o Clipper encontra os melhores momentos, corta em **formato vertical 9:16**, coloca **legendas dinâmicas** e, se você quiser, publica como **YouTube Shorts**.

Os momentos são escolhidos por sinais do próprio vídeo: picos de volume, movimento, chat de lives, o gráfico "mais repetidos" do YouTube e palavras-chave faladas.

## ⬇️ Baixar e usar (Windows 10/11, sem terminal)

1. Abra a página de **[Releases](../../releases/latest)** deste repositório e baixe o arquivo **`Clipper-Windows.zip`**.
2. Clique com o botão direito no zip → **Extrair tudo…** (não rode de dentro do zip).
3. Abra a pasta `Clipper` e dê dois cliques em **`Clipper.exe`**.
4. Cole o link do vídeo, escolha as opções e clique em **✂ Gerar cortes**.

**Aviso do Windows ("O Windows protegeu seu computador"):** o programa não tem assinatura digital paga, então o SmartScreen avisa. Clique em **Mais informações → Executar assim mesmo**.

**Na primeira vez** que você gera cortes com legenda, o programa baixa o modelo de transcrição (precisa de internet): *tiny* ≈ 75 MB, *base* ≈ 145 MB, *small* ≈ 480 MB (padrão), *medium* ≈ 1,5 GB. Isso acontece uma vez só.

### Onde ficam os arquivos

Tudo fica em `C:\Users\<seu usuário>\Clipper`:

| Pasta/arquivo | O que é |
|---|---|
| `clips\` | os cortes prontos (use o botão **Abrir pasta dos cortes**) |
| `models\` | modelos de transcrição baixados |
| `clipper.db` | registro do que já foi processado/publicado (evita cortes repetidos) |
| `settings.json` | suas últimas opções |

Para desinstalar, apague a pasta `Clipper` extraída e a pasta acima.

## 📤 Publicar no YouTube (opcional)

Precisa de um `client_secret.json` grátis, criado por você (cada pessoa usa a própria cota):

1. Acesse o [Google Cloud Console](https://console.cloud.google.com) e crie um projeto.
2. Ative a **YouTube Data API v3**.
3. Em **Tela de consentimento OAuth**, escolha *Externo* e adicione o seu e-mail como usuário de teste.
4. Em **Credenciais**, crie um **ID do cliente OAuth** do tipo **App para computador** e baixe o JSON.
5. No programa, aba **2. Cortes e YouTube** → **Escolher client_secret.json…**.
6. Selecione os cortes e clique em **Publicar selecionados**. Na primeira vez o navegador abre para você autorizar.

Os vídeos sobem como **Privado**: projetos novos da API só podem publicar assim até passarem pela auditoria do Google. A cota padrão permite cerca de 6 envios por dia.

## 💡 Dicas e problemas comuns

- **"Sign in to confirm you're not a bot" / falha ao baixar:** em *Cookies do navegador*, escolha o navegador onde você está logado no YouTube. No Windows o **Firefox** é o que funciona melhor (Chrome e Edge costumam bloquear a leitura de cookies).
- **Poucos ou nenhum corte:** desmarque *Só momentos muito fortes* ou mude o *Tipo de vídeo*.
- **Está lento:** a transcrição roda no processador. Use *Qualidade da legenda → Rápida* ou desmarque as legendas. O tipo *Conversa / podcast* transcreve o vídeo inteiro e demora bem mais.
- **O vídeo já foi processado:** desmarque *Pular vídeos já processados*.

## 🛠️ Para quem quer mexer no código

```bash
pip install -r requirements.txt
python gui.py                                   # abre a janela
python clipper.py --url LINK --len 90 --n 4     # modo linha de comando (--help mostra tudo)
```

Precisa do **ffmpeg** (com libass) no PATH e do **[deno](https://deno.com)** (o yt-dlp usa para falar com o YouTube).

### Gerar o `.exe` você mesmo

No GitHub (recomendado, não precisa instalar nada):

1. Vá em **Releases → Draft a new release**, crie uma tag nova (ex.: `v1.0.0`) e publique.
2. A aba **Actions** monta o programa, testa e anexa o `Clipper-Windows.zip` à release (leva uns 15 minutos).
3. Também dá para rodar em **Actions → Gerar Clipper para Windows → Run workflow**; o zip fica como "artifact" da execução.

No seu Windows:

```bat
pip install -r requirements.txt pyinstaller
pyinstaller Clipper.spec --noconfirm
powershell -ExecutionPolicy Bypass -File scripts\fetch_tools.ps1
dist\Clipper\Clipper.exe --selftest
```

`--selftest` verifica se tudo que o programa precisa foi embutido e grava o relatório em `Clipper\selftest.txt`.

## ⚖️ Avisos

- **Use só conteúdo seu ou que o dono autorizou.** Baixar e republicar vídeos de terceiros sem permissão viola direitos autorais e os termos do YouTube, e pode derrubar canais. A ferramenta é neutra, a responsabilidade pelo uso é sua.
- **Licenças de terceiros embutidas no pacote:** [ffmpeg](https://ffmpeg.org) (build GPL da [BtbN](https://github.com/BtbN/FFmpeg-Builds); texto da licença em `bin\FFMPEG-LICENSE.txt`), [deno](https://deno.com) (MIT), [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense), [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT) e a fonte [Anton](https://fonts.google.com/specimen/Anton) (SIL OFL, em `fonts\OFL.txt`).
- Este repositório ainda não tem uma licença para o código do Clipper. Adicione uma (ex.: pelo botão **Add file → Create new file → LICENSE** no GitHub) antes de divulgar.
