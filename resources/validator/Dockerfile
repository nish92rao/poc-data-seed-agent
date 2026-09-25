ARG LAMBDA_ARCH=x86_64
FROM public.ecr.aws/lambda/nodejs:20-${LAMBDA_ARCH}

COPY package.json package-lock.json ${LAMBDA_TASK_ROOT}/
RUN npm ci --omit=dev --ignore-scripts

COPY handler.js artifact_content_validator.js ${LAMBDA_TASK_ROOT}/

CMD ["handler.handler"]
