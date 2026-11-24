FROM public.ecr.aws/lambda/python:3.12

COPY requirements-api.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements-api.txt \
    --target ${LAMBDA_TASK_ROOT}

COPY app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
