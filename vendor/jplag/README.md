# JPlag vendor jar

Put the real JPlag CLI jar here:

```text
vendor/jplag/jplag.jar
```

Recommended version for this project: JPlag `v6.0.0`, because it runs on JDK 21. The backend Dockerfile now copies a JDK 21 runtime from `eclipse-temurin:21-jdk`, so Java-language code checks have `javac` available.

Download manually:

```bash
curl -L -o vendor/jplag/jplag.jar   https://github.com/jplag/JPlag/releases/download/v6.0.0/jplag-6.0.0-jar-with-dependencies.jar

java -jar vendor/jplag/jplag.jar --help
```

Or run:

```bash
bash deploy/scripts/download_jplag.sh
```

Do not rename the final file. The app config expects `vendor/jplag/jplag.jar`.
